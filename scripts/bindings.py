#!/usr/bin/env python3
"""Safely install, change, inspect, or remove Quick Ask's Hyprland binding."""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import time
from typing import Sequence


PLUGIN_ID = "damianpoole.ask"
DEFAULT_KEY = "SUPER + GRAVE"
BEGIN_MARKER = "-- BEGIN Quick Ask managed binding"
END_MARKER = "-- END Quick Ask managed binding"
ACTION = f"omarchy-shell shell toggle {PLUGIN_ID}"

MAX_BINDINGS_BYTES = 1024 * 1024
MAX_KEY_CHARACTERS = 128
MAX_COMMAND_STDOUT_BYTES = 256 * 1024
MAX_COMMAND_STDERR_BYTES = 16 * 1024
COMMAND_TIMEOUT_SECONDS = 10.0
TERM_GRACE_SECONDS = 0.75
KEY_PATTERN = re.compile(r"^[A-Z0-9_-]+(?:\s*\+\s*[A-Z0-9_-]+)*$")
ANSI_ESCAPE = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
RENAME_EXCHANGE = 2


class BindingError(RuntimeError):
    """A safe, user-presentable binding-management failure."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass
class OpenRegularFile:
    fd: int
    data: bytes
    metadata: os.stat_result

    def close(self) -> None:
        os.close(self.fd)


def _file_version(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _exchange_version(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    """Version fields that remain stable when renameat2 exchanges an entry."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
    )


def _rename_exchange(
    source_directory_fd: int,
    source_name: str,
    destination_directory_fd: int,
    destination_name: str,
) -> None:
    """Atomically exchange two directory entries using Linux renameat2."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise BindingError("Linux renameat2 support is required for safe atomic replacement")
    result = renameat2(
        source_directory_fd,
        os.fsencode(source_name),
        destination_directory_fd,
        os.fsencode(destination_name),
        RENAME_EXCHANGE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _clean_output(data: bytes, limit: int = MAX_COMMAND_STDERR_BYTES) -> str:
    cleaned = ANSI_ESCAPE.sub(b"", data[-limit:]).decode("utf-8", errors="replace")
    return "".join(
        character for character in cleaned if character in "\n\t" or ord(character) >= 0x20
    ).strip()


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def run_bounded_command(
    command: Sequence[str],
    *,
    timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
    stdout_limit: int = MAX_COMMAND_STDOUT_BYTES,
    stderr_limit: int = MAX_COMMAND_STDERR_BYTES,
) -> CommandResult:
    """Run a fixed command with byte limits, a deadline, and group cleanup."""

    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise BindingError(f"could not start {command[0]}: {error}") from error

    assert process.stdout is not None
    assert process.stderr is not None
    os.set_blocking(process.stdout.fileno(), False)
    os.set_blocking(process.stderr.fileno(), False)
    output = bytearray()
    errors = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, (output, stdout_limit, "stdout"))
    selector.register(process.stderr, selectors.EVENT_READ, (errors, stderr_limit, "stderr"))
    deadline = time.monotonic() + timeout_seconds

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BindingError(f"{command[0]} exceeded its {timeout_seconds:g}-second deadline")
            for key, _events in selector.select(min(remaining, 0.1)):
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 8192)
                except (BlockingIOError, InterruptedError):
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                buffer, limit, stream_name = key.data
                if len(buffer) + len(chunk) > limit:
                    raise BindingError(
                        f"{command[0]} {stream_name} exceeded the {limit}-byte limit"
                    )
                buffer.extend(chunk)

        remaining = max(0.0, deadline - time.monotonic())
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise BindingError(
                f"{command[0]} exceeded its {timeout_seconds:g}-second deadline"
            ) from error
        # Do not permit a successful command to leave descendants behind.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return CommandResult(returncode, bytes(output), bytes(errors))
    except BaseException:
        _terminate_process_group(process)
        raise
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


class AnchoredBindingFile:
    """Access one regular file relative to a verified, open parent directory."""

    def __init__(self, path: Path):
        self.path = Path(os.path.abspath(os.fspath(path)))
        parts = self.path.parts
        if len(parts) < 2 or not self.path.name:
            raise BindingError(f"invalid bindings file path: {path}")

        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        directory_fd = os.open(parts[0], directory_flags)
        try:
            for component in parts[1:-1]:
                try:
                    next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
                except OSError as error:
                    raise BindingError(
                        f"unsafe parent chain for {self.path}: "
                        f"{component!r} is not a real directory"
                    ) from error
                os.close(directory_fd)
                directory_fd = next_fd

            parent_metadata = os.fstat(directory_fd)
            if parent_metadata.st_uid != os.geteuid():
                raise BindingError(f"refusing to use {self.path}: parent is not user-owned")
            if stat.S_IMODE(parent_metadata.st_mode) & 0o022:
                raise BindingError(
                    f"refusing to use {self.path}: parent is group- or world-writable"
                )
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise BindingError(
                    f"refusing to use {self.path}: another binding update is in progress"
                ) from error
        except BaseException:
            os.close(directory_fd)
            raise

        self.directory_fd = directory_fd
        self.name = self.path.name

    def __enter__(self) -> AnchoredBindingFile:
        return self

    def __exit__(self, *_error: object) -> None:
        os.close(self.directory_fd)

    def open_regular(self, *, missing_ok: bool = False) -> OpenRegularFile | None:
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            file_fd = os.open(self.name, flags, dir_fd=self.directory_fd)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise BindingError(f"{self.path} does not exist") from None
        except OSError as error:
            raise BindingError(
                f"refusing to read {self.path}: it is a symlink or cannot be opened safely"
            ) from error

        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise BindingError(f"refusing to read {self.path}: not a regular file")
            if before.st_uid != os.geteuid():
                raise BindingError(f"refusing to read {self.path}: file is not user-owned")
            if stat.S_IMODE(before.st_mode) & 0o022:
                raise BindingError(
                    f"refusing to read {self.path}: file is group- or world-writable"
                )
            if before.st_size > MAX_BINDINGS_BYTES:
                raise BindingError(
                    f"refusing to read {self.path}: exceeds the {MAX_BINDINGS_BYTES}-byte limit"
                )

            data = bytearray()
            while len(data) <= MAX_BINDINGS_BYTES:
                chunk = os.read(file_fd, min(8192, MAX_BINDINGS_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > MAX_BINDINGS_BYTES:
                raise BindingError(
                    f"refusing to read {self.path}: exceeds the {MAX_BINDINGS_BYTES}-byte limit"
                )

            after = os.fstat(file_fd)
            if _file_version(before) != _file_version(after):
                raise BindingError(f"refusing to use {self.path}: file changed while being read")
            return OpenRegularFile(file_fd, bytes(data), after)
        except BaseException:
            os.close(file_fd)
            raise

    def assert_current(self, expected: os.stat_result) -> None:
        try:
            current = os.stat(self.name, dir_fd=self.directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            raise BindingError(f"refusing to replace {self.path}: file disappeared") from None
        if not stat.S_ISREG(current.st_mode):
            raise BindingError(f"refusing to replace {self.path}: no longer a regular file")
        if _file_version(current) != _file_version(expected):
            raise BindingError(f"refusing to replace {self.path}: file changed during update")

    def create_backup(self, opened: OpenRegularFile) -> Path:
        self.assert_current(opened.metadata)
        stamp = time.strftime("%Y%m%d%H%M%S")
        for _attempt in range(128):
            backup_name = (
                f"{self.name}.bak.quick-ask-{stamp}-"
                f"{time.time_ns() % 1_000_000_000:09d}-{secrets.token_hex(4)}"
            )
            try:
                backup_fd = self._create_file(backup_name, opened.data, 0o600)
            except FileExistsError:
                continue
            os.close(backup_fd)
            os.fsync(self.directory_fd)
            return self.path.with_name(backup_name)
        raise BindingError(f"could not create a unique backup beside {self.path}")

    def atomic_replace(
        self,
        expected: os.stat_result,
        data: bytes,
        mode: int,
    ) -> os.stat_result:
        if len(data) > MAX_BINDINGS_BYTES:
            raise BindingError(f"updated bindings exceed the {MAX_BINDINGS_BYTES}-byte limit")
        temporary_name = f".{self.name}.tmp.quick-ask-{secrets.token_hex(8)}"
        temporary_fd = self._create_file(temporary_name, data, mode)
        exchanged = False
        try:
            self.assert_current(expected)
            new_metadata = os.fstat(temporary_fd)
            _rename_exchange(
                self.directory_fd,
                temporary_name,
                self.directory_fd,
                self.name,
            )
            exchanged = True

            replaced_metadata = os.stat(
                temporary_name,
                dir_fd=self.directory_fd,
                follow_symlinks=False,
            )
            current_metadata = os.stat(
                self.name,
                dir_fd=self.directory_fd,
                follow_symlinks=False,
            )
            if _exchange_version(replaced_metadata) != _exchange_version(expected):
                if _exchange_version(current_metadata) == _exchange_version(new_metadata):
                    _rename_exchange(
                        self.directory_fd,
                        temporary_name,
                        self.directory_fd,
                        self.name,
                    )
                    exchanged = False
                    raise BindingError(
                        f"refusing to replace {self.path}: file changed during atomic update"
                    )
                os.chmod(
                    temporary_name,
                    0o600,
                    dir_fd=self.directory_fd,
                    follow_symlinks=False,
                )
                os.fsync(self.directory_fd)
                retained_path = self.path.with_name(temporary_name)
                raise BindingError(
                    f"refusing to replace {self.path}: multiple concurrent updates occurred; "
                    f"displaced content was retained at {retained_path}"
                )
            if _exchange_version(current_metadata) != _exchange_version(new_metadata):
                exchanged = False
                raise BindingError(
                    f"refusing to replace {self.path}: replacement was concurrently superseded"
                )

            os.unlink(temporary_name, dir_fd=self.directory_fd)
            exchanged = False
            os.fsync(self.directory_fd)
            return current_metadata
        finally:
            os.close(temporary_fd)
            if not exchanged:
                try:
                    os.unlink(temporary_name, dir_fd=self.directory_fd)
                except FileNotFoundError:
                    pass

    def _create_file(self, name: str, data: bytes, mode: int) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        file_fd = os.open(name, flags, 0o600, dir_fd=self.directory_fd)
        try:
            view = memoryview(data)
            while view:
                written = os.write(file_fd, view)
                if written == 0:
                    raise OSError("short write while creating file")
                view = view[written:]
            os.fchmod(file_fd, mode & 0o777)
            os.fsync(file_fd)
            return file_fd
        except BaseException:
            os.close(file_fd)
            try:
                os.unlink(name, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass
            raise


def bindings_path() -> Path:
    return Path.home() / ".config" / "hypr" / "bindings.lua"


def validate_key(key: str) -> str:
    normalized = " + ".join(part.strip().upper() for part in key.split("+"))
    if (
        not normalized
        or len(normalized) > MAX_KEY_CHARACTERS
        or not KEY_PATTERN.fullmatch(normalized)
    ):
        raise BindingError(
            "invalid key; use names joined by '+', for example SUPER + CTRL + K"
        )
    return normalized


def managed_block(key: str) -> str:
    return (
        f'{BEGIN_MARKER}\n'
        f'o.bind("{key}", "Quick Ask", "{ACTION}")\n'
        f"{END_MARKER}"
    )


def managed_range(text: str) -> tuple[int, int] | None:
    begin_count = text.count(BEGIN_MARKER)
    end_count = text.count(END_MARKER)
    if begin_count == 0 and end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise BindingError("bindings file contains duplicate or incomplete Quick Ask markers")

    start = text.index(BEGIN_MARKER)
    end_start = text.index(END_MARKER)
    if end_start <= start:
        raise BindingError("Quick Ask binding markers are out of order")
    end = end_start + len(END_MARKER)
    if (start > 0 and text[start - 1] != "\n") or (
        end < len(text) and text[end] != "\n"
    ):
        raise BindingError("Quick Ask binding markers must occupy complete lines")
    return start, end


def installed_block(text: str) -> str | None:
    location = managed_range(text)
    if location is None:
        return None
    return text[location[0] : location[1]]


def installed_key(text: str) -> str | None:
    block = installed_block(text)
    if block is None:
        return None
    pattern = re.compile(
        rf'^o\.bind\("([A-Z0-9_+ -]+)", "Quick Ask", "{re.escape(ACTION)}"\)$',
        re.MULTILINE,
    )
    match = pattern.search(block)
    if match is None:
        raise BindingError("the managed Quick Ask binding block is malformed")
    key = validate_key(match.group(1))
    if block != managed_block(key):
        raise BindingError("the managed Quick Ask binding block contains unexpected content")
    return key


def legacy_binding(text: str) -> tuple[str, int, int] | None:
    """Find the exact unmarked binding documented by Quick Ask 1.5 and earlier."""

    pattern = re.compile(
        rf'^o\.bind\("([A-Za-z0-9_+ -]+)", "Quick Ask", "{re.escape(ACTION)}"\)\n?',
        re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    if len(matches) != 1:
        raise BindingError("bindings file contains duplicate unmarked Quick Ask bindings")

    match = matches[0]
    key = validate_key(match.group(1))
    start, end = match.span()
    prefix = text[:start]
    unbind_pattern = re.compile(
        rf'^hl\.unbind\("{re.escape(match.group(1))}"\)\n$',
        re.MULTILINE,
    )
    unbind_matches = list(unbind_pattern.finditer(prefix))
    if unbind_matches and unbind_matches[-1].end() == len(prefix):
        start = unbind_matches[-1].start()
    return key, start, end


def remove_managed_block(text: str) -> tuple[str, bool]:
    location = managed_range(text)
    if location is None:
        return text, False
    start, end = location
    if end < len(text) and text[end] == "\n":
        end += 1
    if start > 0 and text[start - 1] == "\n" and end == len(text):
        start -= 1
    return text[:start] + text[end:], True


def remove_owned_binding(text: str) -> tuple[str, bool]:
    if managed_range(text) is not None:
        return remove_managed_block(text)
    legacy = legacy_binding(text)
    if legacy is None:
        return text, False
    _key, start, end = legacy
    return text[:start] + text[end:], True


def decode_bindings(opened: OpenRegularFile, path: Path) -> str:
    try:
        return opened.data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BindingError(f"{path} is not valid UTF-8") from error


def normalize_key(key: str) -> str:
    return " ".join(key.replace("+", " + ").split()).upper()


def ensure_key_is_free(key: str) -> None:
    result = run_bounded_command(
        ["omarchy", "menu", "keybindings", "--print"],
        timeout_seconds=5.0,
    )
    if result.returncode != 0:
        detail = _clean_output(result.stderr) or f"exit status {result.returncode}"
        raise BindingError(f"could not inspect existing Omarchy keybindings: {detail}")

    wanted = normalize_key(key)
    output = _clean_output(result.stdout, MAX_COMMAND_STDOUT_BYTES)
    for line in output.splitlines():
        left, separator, right = line.partition("→")
        if separator and normalize_key(left) == wanted:
            description = right.strip() or "another action"
            raise BindingError(
                f"{key} is already bound to {description}; choose another key with 'set'"
            )


def validate_hyprland() -> None:
    reload_result = run_bounded_command(["hyprctl", "reload"])
    if reload_result.returncode != 0:
        detail = _clean_output(reload_result.stderr) or _clean_output(reload_result.stdout)
        raise BindingError(f"hyprctl reload failed: {detail or reload_result.returncode}")

    errors_result = run_bounded_command(["hyprctl", "configerrors"])
    errors = _clean_output(
        errors_result.stdout + (b"\n" if errors_result.stdout else b"") + errors_result.stderr,
        MAX_COMMAND_STDOUT_BYTES,
    )
    if errors_result.returncode != 0:
        raise BindingError(
            f"hyprctl configerrors failed: {errors or errors_result.returncode}"
        )
    if errors and errors.lower() not in {"ok", "no config errors"}:
        raise BindingError(f"Hyprland reported configuration errors: {errors}")


def _validate_or_rollback(
    target: AnchoredBindingFile,
    updated_metadata: os.stat_result,
    original: OpenRegularFile,
    backup_path: Path,
) -> None:
    try:
        validate_hyprland()
        target.assert_current(updated_metadata)
        return
    except BindingError as validation_error:
        try:
            target.atomic_replace(
                updated_metadata,
                original.data,
                stat.S_IMODE(original.metadata.st_mode),
            )
            validate_hyprland()
        except (BindingError, OSError) as rollback_error:
            raise BindingError(
                f"Hyprland validation failed ({validation_error}); automatic rollback also "
                f"failed ({rollback_error}). Restore {backup_path} manually."
            ) from rollback_error
        raise BindingError(
            f"Hyprland validation failed and the change was rolled back: {validation_error} "
            f"(backup: {backup_path})"
        ) from validation_error


def update_binding(path: Path, requested_key: str | None, *, live: bool) -> int:
    with AnchoredBindingFile(path) as target:
        opened = target.open_regular()
        assert opened is not None
        try:
            text = decode_bindings(opened, target.path)
            current_key = installed_key(text)
            if current_key is None:
                legacy = legacy_binding(text)
                current_key = legacy[0] if legacy else None
            key = validate_key(requested_key or current_key or DEFAULT_KEY)
            desired_block = managed_block(key)
            if installed_block(text) == desired_block:
                print(f"Quick Ask keybinding is already installed: {key}")
                return 0

            updated_text, owned = remove_owned_binding(text)
            if live:
                validate_hyprland()
                if current_key != key:
                    ensure_key_is_free(key)

            if not updated_text or updated_text.endswith("\n\n"):
                separator = ""
            elif updated_text.endswith("\n"):
                separator = "\n"
            else:
                separator = "\n\n"
            updated_data = (updated_text + separator + desired_block + "\n").encode("utf-8")
            if len(updated_data) > MAX_BINDINGS_BYTES:
                raise BindingError(
                    f"updated bindings exceed the {MAX_BINDINGS_BYTES}-byte limit"
                )

            backup_path = target.create_backup(opened)
            updated_metadata = target.atomic_replace(
                opened.metadata,
                updated_data,
                stat.S_IMODE(opened.metadata.st_mode),
            )
            if live:
                _validate_or_rollback(target, updated_metadata, opened, backup_path)
        finally:
            opened.close()

    action = "Updated" if owned else "Installed"
    suffix = "" if live else " (test file; Hyprland was not touched)"
    print(f"{action} Quick Ask keybinding: {key}{suffix}")
    print(f"Backup: {backup_path}")
    return 0


def remove_binding(path: Path, *, live: bool) -> int:
    with AnchoredBindingFile(path) as target:
        opened = target.open_regular(missing_ok=True)
        if opened is None:
            print("Hyprland bindings file is absent; nothing to remove.")
            return 0
        try:
            text = decode_bindings(opened, target.path)
            # Validate owned content before removing it.
            managed_key = installed_key(text)
            if managed_key is None:
                legacy_binding(text)
            updated_text, changed = remove_owned_binding(text)
            if not changed:
                print("No managed Quick Ask keybinding is installed.")
                return 0
            if live:
                validate_hyprland()

            backup_path = target.create_backup(opened)
            updated_metadata = target.atomic_replace(
                opened.metadata,
                updated_text.encode("utf-8"),
                stat.S_IMODE(opened.metadata.st_mode),
            )
            if live:
                _validate_or_rollback(target, updated_metadata, opened, backup_path)
        finally:
            opened.close()

    suffix = "" if live else " (test file; Hyprland was not touched)"
    print(f"Removed Quick Ask keybinding{suffix}.")
    print(f"Backup: {backup_path}")
    return 0


def binding_status(path: Path) -> int:
    with AnchoredBindingFile(path) as target:
        opened = target.open_regular(missing_ok=True)
        if opened is None:
            print("No managed Quick Ask keybinding is installed.")
            return 0
        try:
            key = installed_key(decode_bindings(opened, target.path))
        finally:
            opened.close()
    if key:
        print(f"Managed Quick Ask keybinding: {key}")
    else:
        print("No managed Quick Ask keybinding is installed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "set", "status", "remove"))
    parser.add_argument("key", nargs="?", help='key combination, such as "SUPER + CTRL + K"')
    parser.add_argument(
        "--file",
        type=Path,
        default=bindings_path(),
        help="alternate bindings.lua path for testing (does not invoke Omarchy or Hyprland)",
    )
    arguments = parser.parse_args()
    path = Path(os.path.abspath(os.fspath(arguments.file)))
    live = path == Path(os.path.abspath(os.fspath(bindings_path())))

    try:
        if arguments.action == "set":
            if not arguments.key:
                raise BindingError('set requires a key, such as "SUPER + CTRL + K"')
            return update_binding(path, arguments.key, live=live)
        if arguments.key:
            raise BindingError(f"{arguments.action} does not accept a key argument")
        if arguments.action == "install":
            return update_binding(path, None, live=live)
        if arguments.action == "remove":
            return remove_binding(path, live=live)
        return binding_status(path)
    except (BindingError, OSError, subprocess.SubprocessError) as error:
        print(f"bindings.py: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
