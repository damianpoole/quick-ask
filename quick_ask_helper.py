#!/usr/bin/env python3
"""Bounded, stdin-only bridge between Quick Ask and supported agent CLIs."""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import resource
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable, Sequence


MAX_REQUEST_BYTES = 256 * 1024
MAX_PROMPT_BYTES = 128 * 1024
MAX_ANSWER_BYTES = 256 * 1024
MAX_AGENT_STDOUT_BYTES = MAX_ANSWER_BYTES
MAX_AGENT_STDERR_BYTES = 16 * 1024
MAX_DETECT_STDOUT_BYTES = 128
MAX_DETECT_STDERR_BYTES = 2 * 1024
ASK_TIMEOUT_SECONDS = 120.0
DETECT_TIMEOUT_SECONDS = 2.0
TERM_GRACE_SECONDS = 1.5
KILL_GRACE_SECONDS = 1.0
SUPPORTED_AGENTS = frozenset({"codex", "claude"})

COMMON_ENVIRONMENT_KEYS = frozenset(
    {
        "ALL_PROXY",
        "all_proxy",
        "HOME",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "LANG",
        "LOGNAME",
        "NO_PROXY",
        "no_proxy",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "TMPDIR",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    }
)
AGENT_ENVIRONMENT_KEYS = {
    "codex": frozenset({"CODEX_HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL"}),
    "claude": frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CONFIG_DIR",
        }
    ),
}

_ANSI_ESCAPE = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_active_process: subprocess.Popen[bytes] | None = None
_cancel_requested = False


class BridgeError(RuntimeError):
    """A bounded, user-presentable bridge failure."""


class OutputOverflow(BridgeError):
    pass


class ProcessDeadline(BridgeError):
    pass


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    captured: bytes = b""


def _enable_subreaper() -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_CHILD_SUBREAPER)")
    except (AttributeError, OSError) as error:
        raise BridgeError("Linux child-subreaper support is required for safe cleanup.") from error


def _child_setup() -> Callable[[], None]:
    expected_parent = os.getpid()

    def setup() -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG)")
        if os.getppid() != expected_parent:
            os.kill(os.getpid(), signal.SIGKILL)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return setup


def _signal_process_group(process: subprocess.Popen[bytes], signum: int) -> None:
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def _adopted_child_pids() -> list[int]:
    try:
        children = Path(f"/proc/self/task/{os.getpid()}/children").read_text("ascii")
    except (OSError, UnicodeError):
        return []
    return [int(value) for value in children.split() if value.isdigit()]


def _cleanup_adopted_children(deadline: float) -> None:
    while time.monotonic() < deadline:
        for child_pid in _adopted_child_pids():
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        live_children = False
        while True:
            try:
                child_pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if child_pid == 0:
                live_children = bool(_adopted_child_pids())
                break
        if not live_children:
            return
        time.sleep(0.01)


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    _signal_process_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_process_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    # The group may still contain descendants after the leader exits.
    _signal_process_group(process, signal.SIGKILL)
    _cleanup_adopted_children(time.monotonic() + KILL_GRACE_SECONDS)


def _handle_signal(_signum: int, _frame: object) -> None:
    global _cancel_requested
    _cancel_requested = True
    if _active_process is not None:
        _signal_process_group(_active_process, signal.SIGTERM)


def _append_bounded(buffer: bytearray, chunk: bytes, limit: int, stream_name: str) -> None:
    if len(buffer) + len(chunk) > limit:
        raise OutputOverflow(f"Agent {stream_name} exceeded the {limit}-byte limit.")
    buffer.extend(chunk)


def run_bounded(
    command: Sequence[str],
    *,
    stdin_data: bytes | bytearray,
    timeout_seconds: float,
    stdout_limit: int,
    stderr_limit: int,
    pass_fds: Sequence[int] = (),
    environment: dict[str, str] | None = None,
    working_directory: str | None = None,
    captured_output: tuple[BinaryIO, int, str] | None = None,
    close_after_spawn: Sequence[int] = (),
) -> ProcessResult:
    global _active_process
    if _cancel_requested:
        raise BridgeError("Request cancelled.")
    started = time.monotonic()
    deadline = started + timeout_seconds
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        pass_fds=tuple(pass_fds),
        preexec_fn=_child_setup(),
        env=environment,
        cwd=working_directory,
    )
    for file_descriptor in close_after_spawn:
        os.close(file_descriptor)
    _active_process = process

    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    captured_buffer = bytearray()
    parser = selectors.DefaultSelector()
    root_exit_cleaned = False
    input_view = memoryview(stdin_data)
    input_offset = 0
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        if input_view:
            parser.register(process.stdin, selectors.EVENT_WRITE, ("stdin",))
        else:
            process.stdin.close()
        parser.register(process.stdout, selectors.EVENT_READ, ("output", stdout_buffer, stdout_limit, "stdout"))
        parser.register(process.stderr, selectors.EVENT_READ, ("output", stderr_buffer, stderr_limit, "stderr"))
        if captured_output is not None:
            stream, limit, stream_name = captured_output
            os.set_blocking(stream.fileno(), False)
            parser.register(stream, selectors.EVENT_READ, ("output", captured_buffer, limit, stream_name))

        while parser.get_map():
            if _cancel_requested:
                raise BridgeError("Request cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessDeadline(f"Agent exceeded the {int(timeout_seconds)}-second deadline.")

            if process.poll() is not None and not root_exit_cleaned:
                # A completed CLI must not leave helpers holding our pipes open.
                _signal_process_group(process, signal.SIGTERM)
                if process.stdin is not None and not process.stdin.closed:
                    try:
                        parser.unregister(process.stdin)
                    except KeyError:
                        pass
                    process.stdin.close()
                root_exit_cleaned = True

            for key, _events in parser.select(min(remaining, 0.1)):
                stream = key.fileobj
                if key.data[0] == "stdin":
                    try:
                        written = os.write(stream.fileno(), input_view[input_offset:])
                    except (BlockingIOError, InterruptedError):
                        continue
                    except BrokenPipeError as error:
                        raise BridgeError("Agent closed its private input channel.") from error
                    if written == 0:
                        raise BridgeError("Agent stopped accepting private input.")
                    input_offset += written
                    if input_offset == len(input_view):
                        parser.unregister(stream)
                        stream.close()
                    continue
                try:
                    chunk = os.read(stream.fileno(), 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    parser.unregister(stream)
                    stream.close()
                    continue
                _kind, buffer, limit, stream_name = key.data
                _append_bounded(buffer, chunk, limit, stream_name)

        remaining = max(0.0, deadline - time.monotonic())
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise ProcessDeadline(f"Agent exceeded the {int(timeout_seconds)}-second deadline.") from error
        _signal_process_group(process, signal.SIGKILL)
        _cleanup_adopted_children(time.monotonic() + KILL_GRACE_SECONDS)
        return ProcessResult(
            returncode,
            bytes(stdout_buffer),
            bytes(stderr_buffer),
            bytes(captured_buffer),
        )
    except BaseException:
        _terminate_process_tree(process)
        raise
    finally:
        parser.close()
        extra_stream = captured_output[0] if captured_output is not None else None
        for stream in (process.stdin, process.stdout, process.stderr, extra_stream):
            if stream is not None and not stream.closed:
                stream.close()
        _active_process = None


def _clean_text(data: bytes, limit: int) -> str:
    data = _ANSI_ESCAPE.sub(b"", data[-limit:])
    text = data.decode("utf-8", errors="replace")
    return "".join(character for character in text if character in "\n\t" or ord(character) >= 0x20).strip()


def _find_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise BridgeError(f"The configured {name} CLI is not installed.")
    # Preserve the discovered basename for multi-call launchers such as mise.
    # Resolving a `codex -> mise` shim to its real target changes argv[0], which
    # makes mise consume Codex's flags itself instead of dispatching to Codex.
    return os.path.abspath(executable)


def _agent_environment(agent: str | None = None) -> dict[str, str]:
    """Return only environment values required to locate and authenticate a CLI."""

    allowed = set(COMMON_ENVIRONMENT_KEYS)
    allowed.update(key for key in os.environ if key == "LC_ALL" or key.startswith("LC_"))
    if agent is not None:
        allowed.update(AGENT_ENVIRONMENT_KEYS.get(agent, ()))
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return environment


def detect_agent() -> str:
    result = run_bounded(
        [_find_executable("omarchy-default-agent")],
        stdin_data=b"",
        timeout_seconds=DETECT_TIMEOUT_SECONDS,
        stdout_limit=MAX_DETECT_STDOUT_BYTES,
        stderr_limit=MAX_DETECT_STDERR_BYTES,
        environment=_agent_environment(),
    )
    if result.returncode != 0:
        detail = _clean_text(result.stderr, MAX_DETECT_STDERR_BYTES)
        raise BridgeError(detail or "Could not determine the default Omarchy agent.")
    agent = _clean_text(result.stdout, MAX_DETECT_STDOUT_BYTES)
    if not agent:
        raise BridgeError("No default agent is configured. Run: omarchy default agent <name>")
    if not _AGENT_NAME.fullmatch(agent):
        raise BridgeError("The configured default agent name is invalid.")
    if agent not in SUPPORTED_AGENTS:
        supported = ", ".join(sorted(SUPPORTED_AGENTS))
        raise BridgeError(f"Quick Ask supports {supported}; the configured agent is {agent}.")
    return agent


def _read_request() -> bytearray:
    raw = bytearray(sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1))
    if len(raw) > MAX_REQUEST_BYTES:
        raise BridgeError(f"Request exceeded the {MAX_REQUEST_BYTES}-byte transport limit.")
    if not raw or raw[-1] != 0x0A:
        raise BridgeError("The request was incomplete.")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BridgeError("The request was not valid UTF-8 JSON.") from error
    finally:
        for index in range(len(raw)):
            raw[index] = 0
    if not isinstance(value, dict) or set(value) != {"prompt"} or not isinstance(value["prompt"], str):
        raise BridgeError("The request schema was invalid.")
    prompt_text = value.pop("prompt")
    prompt = bytearray(prompt_text.encode("utf-8"))
    prompt_text = ""
    if not prompt or len(prompt) > MAX_PROMPT_BYTES:
        raise BridgeError(f"Prompt must contain between 1 and {MAX_PROMPT_BYTES} UTF-8 bytes.")
    return prompt


def _run_codex(
    prompt: bytearray,
    timeout_seconds: float,
    *,
    inherit_agent_config: bool,
    working_directory: str,
) -> str:
    answer_read_fd, answer_write_fd = os.pipe2(os.O_CLOEXEC)
    answer_stream = os.fdopen(answer_read_fd, "rb", buffering=0)
    try:
        command = [
            _find_executable("codex"),
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "-s",
            "read-only",
            "--color",
            "never",
            "--output-last-message",
            f"/proc/self/fd/{answer_write_fd}",
            "-",
        ]
        if not inherit_agent_config:
            command[2:2] = ["--ignore-user-config", "--ignore-rules"]
        result = run_bounded(
            command,
            stdin_data=prompt,
            timeout_seconds=timeout_seconds,
            stdout_limit=MAX_AGENT_STDOUT_BYTES,
            stderr_limit=MAX_AGENT_STDERR_BYTES,
            pass_fds=(answer_write_fd,),
            environment=_agent_environment("codex"),
            working_directory=working_directory,
            captured_output=(answer_stream, MAX_ANSWER_BYTES, "answer"),
            close_after_spawn=(answer_write_fd,),
        )
        if result.returncode != 0:
            detail = _clean_text(result.stderr, MAX_AGENT_STDERR_BYTES)
            raise BridgeError(detail or f"Codex exited with status {result.returncode}.")
        answer = result.captured
        if not answer:
            raise BridgeError("Codex returned no answer.")
        return _clean_text(answer, MAX_ANSWER_BYTES)
    finally:
        if not answer_stream.closed:
            answer_stream.close()
        try:
            os.close(answer_write_fd)
        except OSError:
            pass


def _run_claude(
    prompt: bytearray,
    timeout_seconds: float,
    *,
    inherit_agent_config: bool,
    working_directory: str,
) -> str:
    command = [
        _find_executable("claude"),
        "-p",
        "--output-format",
        "text",
        "--permission-mode",
        "plan",
        "--no-session-persistence",
    ]
    if not inherit_agent_config:
        command.extend(
            [
                "--restricted",
                "--tools",
                "",
                "--disallowedTools",
                "mcp__*",
            ]
        )
    result = run_bounded(
        command,
        stdin_data=prompt,
        timeout_seconds=timeout_seconds,
        stdout_limit=MAX_ANSWER_BYTES,
        stderr_limit=MAX_AGENT_STDERR_BYTES,
        environment=_agent_environment("claude"),
        working_directory=working_directory,
    )
    if result.returncode != 0:
        detail = _clean_text(result.stderr, MAX_AGENT_STDERR_BYTES)
        raise BridgeError(detail or f"Claude exited with status {result.returncode}.")
    answer = _clean_text(result.stdout, MAX_ANSWER_BYTES)
    if not answer:
        raise BridgeError("Claude returned no answer.")
    return answer


def ask(*, inherit_agent_config: bool = False) -> dict[str, object]:
    started = time.monotonic()
    agent = detect_agent()
    if _cancel_requested:
        raise BridgeError("Request cancelled.")
    prompt = _read_request()
    try:
        if _cancel_requested:
            raise BridgeError("Request cancelled.")
        remaining = ASK_TIMEOUT_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            raise ProcessDeadline(f"Agent exceeded the {int(ASK_TIMEOUT_SECONDS)}-second deadline.")
        with tempfile.TemporaryDirectory(prefix="quick-ask-agent-") as working_directory:
            if agent == "codex":
                answer = _run_codex(
                    prompt,
                    remaining,
                    inherit_agent_config=inherit_agent_config,
                    working_directory=working_directory,
                )
            elif agent == "claude":
                answer = _run_claude(
                    prompt,
                    remaining,
                    inherit_agent_config=inherit_agent_config,
                    working_directory=working_directory,
                )
            else:  # Kept explicit so adding an allowlist entry cannot bypass an adapter.
                raise BridgeError(f"No private-input adapter exists for {agent}.")
        return {"ok": True, "agent": agent, "answer": answer}
    finally:
        for index in range(len(prompt)):
            prompt[index] = 0


def emit(payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(encoded)
    sys.stdout.write("\n")
    sys.stdout.flush()


def main() -> int:
    global _cancel_requested
    _cancel_requested = False
    try:
        _enable_subreaper()
    except BridgeError as error:
        emit({"ok": False, "error": str(error)[:4096]})
        return 1
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGHUP, _handle_signal)

    valid_detect = sys.argv == [sys.argv[0], "detect"]
    valid_ask = len(sys.argv) in {2, 3} and sys.argv[1] == "ask"
    inherit_agent_config = len(sys.argv) == 3 and sys.argv[2] == "--inherit-agent-config"
    if not valid_detect and not (valid_ask and (len(sys.argv) == 2 or inherit_agent_config)):
        emit(
            {
                "ok": False,
                "error": "Usage: quick_ask_helper.py detect | ask [--inherit-agent-config]",
            }
        )
        return 2
    try:
        if valid_detect:
            emit({"ok": True, "agent": detect_agent()})
        else:
            emit(ask(inherit_agent_config=inherit_agent_config))
        return 0
    except (BridgeError, OSError, subprocess.SubprocessError) as error:
        emit({"ok": False, "error": str(error)[:4096]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
