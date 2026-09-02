from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock


REPO_DIR = Path(__file__).resolve().parents[1]
BINDINGS_PATH = REPO_DIR / "scripts" / "bindings.py"
SPEC = importlib.util.spec_from_file_location("quick_ask_bindings", BINDINGS_PATH)
assert SPEC is not None and SPEC.loader is not None
BINDINGS = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BINDINGS
SPEC.loader.exec_module(BINDINGS)


class BindingManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="quick-ask-bindings-")
        self.root = Path(self.temporary.name)
        self.path = self.root / "bindings.lua"
        self.original = '-- User binding\no.bind("SUPER + B", "Browser", "browser")\n'
        self.path.write_text(self.original, encoding="utf-8")
        self.path.chmod(0o640)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def backups(self) -> list[Path]:
        return list(self.root.glob("bindings.lua.bak.quick-ask-*"))

    def test_install_is_atomic_idempotent_and_preserves_existing_content(self) -> None:
        self.assertEqual(BINDINGS.update_binding(self.path, None, live=False), 0)
        installed = self.path.read_text("utf-8")
        self.assertTrue(installed.startswith(self.original))
        self.assertIn(BINDINGS.managed_block(BINDINGS.DEFAULT_KEY), installed)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text("utf-8"), self.original)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

        self.assertEqual(BINDINGS.update_binding(self.path, None, live=False), 0)
        self.assertEqual(self.path.read_text("utf-8"), installed)
        self.assertEqual(len(self.backups()), 1)

    def test_set_replaces_only_the_owned_block(self) -> None:
        BINDINGS.update_binding(self.path, None, live=False)
        BINDINGS.update_binding(self.path, "super + ctrl + k", live=False)
        updated = self.path.read_text("utf-8")
        self.assertTrue(updated.startswith(self.original))
        self.assertEqual(updated.count(BINDINGS.BEGIN_MARKER), 1)
        self.assertIn(BINDINGS.managed_block("SUPER + CTRL + K"), updated)
        self.assertNotIn(BINDINGS.DEFAULT_KEY, updated)

    def test_remove_preserves_user_content(self) -> None:
        BINDINGS.update_binding(self.path, None, live=False)
        self.assertEqual(BINDINGS.remove_binding(self.path, live=False), 0)
        self.assertEqual(self.path.read_text("utf-8"), self.original)
        self.assertEqual(len(self.backups()), 2)

    def test_legacy_readme_binding_is_migrated_and_removed(self) -> None:
        legacy = (
            self.original
            + 'hl.unbind("SUPER + grave")\n'
            + 'o.bind("SUPER + grave", "Quick Ask", '
            + f'"{BINDINGS.ACTION}")\n'
        )
        self.path.write_text(legacy, encoding="utf-8")

        BINDINGS.update_binding(self.path, None, live=False)
        migrated = self.path.read_text("utf-8")
        self.assertNotIn('hl.unbind("SUPER + grave")', migrated)
        self.assertEqual(migrated.count(BINDINGS.ACTION), 1)
        self.assertIn(BINDINGS.managed_block(BINDINGS.DEFAULT_KEY), migrated)

        BINDINGS.remove_binding(self.path, live=False)
        self.assertEqual(self.path.read_text("utf-8"), self.original)

    def test_oversized_file_is_rejected_without_a_backup(self) -> None:
        self.path.write_bytes(b"x" * (BINDINGS.MAX_BINDINGS_BYTES + 1))
        with self.assertRaisesRegex(BINDINGS.BindingError, "byte limit"):
            BINDINGS.update_binding(self.path, None, live=False)
        self.assertEqual(self.backups(), [])

        near_limit = b"x" * (BINDINGS.MAX_BINDINGS_BYTES - 10)
        self.path.write_bytes(near_limit)
        with self.assertRaisesRegex(BINDINGS.BindingError, "updated bindings exceed"):
            BINDINGS.update_binding(self.path, None, live=False)
        self.assertEqual(self.path.read_bytes(), near_limit)
        self.assertEqual(self.backups(), [])

    def test_symlink_target_and_symlink_parent_are_rejected(self) -> None:
        real = self.root / "real.lua"
        real.write_text(self.original, encoding="utf-8")
        self.path.unlink()
        self.path.symlink_to(real)
        with self.assertRaisesRegex(BINDINGS.BindingError, "symlink"):
            BINDINGS.binding_status(self.path)

        actual_parent = self.root / "actual"
        actual_parent.mkdir()
        nested_file = actual_parent / "bindings.lua"
        nested_file.write_text(self.original, encoding="utf-8")
        alias = self.root / "alias"
        alias.symlink_to(actual_parent, target_is_directory=True)
        with self.assertRaisesRegex(BINDINGS.BindingError, "unsafe parent chain"):
            BINDINGS.binding_status(alias / "bindings.lua")

    def test_writable_parent_and_file_are_rejected(self) -> None:
        self.root.chmod(0o770)
        try:
            with self.assertRaisesRegex(BINDINGS.BindingError, "parent is group- or world-writable"):
                BINDINGS.binding_status(self.path)
        finally:
            self.root.chmod(0o700)

        self.path.chmod(0o660)
        with self.assertRaisesRegex(BINDINGS.BindingError, "file is group- or world-writable"):
            BINDINGS.binding_status(self.path)

    def test_duplicate_or_malformed_markers_fail_closed(self) -> None:
        for content in (
            f"{BINDINGS.BEGIN_MARKER}\n{BINDINGS.BEGIN_MARKER}\n{BINDINGS.END_MARKER}\n",
            f"{BINDINGS.BEGIN_MARKER}\n",
            f"prefix {BINDINGS.BEGIN_MARKER}\n{BINDINGS.END_MARKER}\n",
        ):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(BINDINGS.BindingError):
                    BINDINGS.binding_status(self.path)

    @mock.patch.object(BINDINGS, "ensure_key_is_free")
    @mock.patch.object(BINDINGS, "validate_hyprland")
    def test_successful_live_install_checks_key_and_validates_before_and_after(
        self,
        validate_hyprland: mock.Mock,
        ensure_key_is_free: mock.Mock,
    ) -> None:
        BINDINGS.update_binding(self.path, None, live=True)
        ensure_key_is_free.assert_called_once_with(BINDINGS.DEFAULT_KEY)
        self.assertEqual(validate_hyprland.call_count, 2)
        self.assertIn(BINDINGS.managed_block(BINDINGS.DEFAULT_KEY), self.path.read_text("utf-8"))

    @mock.patch.object(BINDINGS, "ensure_key_is_free")
    @mock.patch.object(BINDINGS, "validate_hyprland")
    def test_occupied_key_does_not_modify_file(
        self,
        validate_hyprland: mock.Mock,
        ensure_key_is_free: mock.Mock,
    ) -> None:
        ensure_key_is_free.side_effect = BINDINGS.BindingError("key is occupied")
        with self.assertRaisesRegex(BINDINGS.BindingError, "occupied"):
            BINDINGS.update_binding(self.path, None, live=True)
        validate_hyprland.assert_called_once_with()
        ensure_key_is_free.assert_called_once_with(BINDINGS.DEFAULT_KEY)
        self.assertEqual(self.path.read_text("utf-8"), self.original)
        self.assertEqual(self.backups(), [])

    @mock.patch.object(BINDINGS, "ensure_key_is_free")
    @mock.patch.object(BINDINGS, "validate_hyprland")
    def test_failed_post_write_validation_rolls_back(
        self,
        validate_hyprland: mock.Mock,
        _ensure_key_is_free: mock.Mock,
    ) -> None:
        validate_hyprland.side_effect = [None, BINDINGS.BindingError("bad config"), None]
        with self.assertRaisesRegex(BINDINGS.BindingError, "rolled back"):
            BINDINGS.update_binding(self.path, None, live=True)
        self.assertEqual(validate_hyprland.call_count, 3)
        self.assertEqual(self.path.read_text("utf-8"), self.original)
        self.assertEqual(len(self.backups()), 1)


class BoundedCommandTests(unittest.TestCase):
    def test_stdout_overflow_terminates_the_producer(self) -> None:
        with self.assertRaisesRegex(BINDINGS.BindingError, "stdout exceeded"):
            BINDINGS.run_bounded_command(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'x' * 4096); sys.stdout.flush()",
                ],
                stdout_limit=1024,
                stderr_limit=1024,
                timeout_seconds=2,
            )

    def test_deadline_terminates_the_process_group(self) -> None:
        started = time.monotonic()
        with self.assertRaisesRegex(BINDINGS.BindingError, "deadline"):
            BINDINGS.run_bounded_command(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdout_limit=1024,
                stderr_limit=1024,
                timeout_seconds=0.2,
            )
        self.assertLess(time.monotonic() - started, 2)


if __name__ == "__main__":
    unittest.main()
