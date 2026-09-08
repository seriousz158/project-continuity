"""Filesystem boundaries and failure ordering for standalone relay storage."""
import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/storage.py"
spec = importlib.util.spec_from_file_location("relay_storage_test", SCRIPT)
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_errors_and_warnings_do_not_echo_oserror_details(self):
        fake = "ghp_" + "D" * 36
        with self.assertRaises(storage.Error) as caught:
            storage.root_path(str(self.root / fake))
        self.assertNotIn(fake, str(caught.exception))
        target = self.root / "CURRENT.md"
        with mock.patch.object(storage.os, "replace", side_effect=OSError(fake)):
            with self.assertRaises(storage.Error) as caught:
                storage.atomic(target, "data")
        self.assertNotIn(fake, str(caught.exception))
        original = storage.os.fsync
        calls = 0
        def fsync(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(fake)
            return original(fd)
        with mock.patch.object(storage.os, "fsync", side_effect=fsync):
            warnings = storage.atomic(target, "committed")
        self.assertTrue(warnings)
        self.assertNotIn(fake, str(warnings))

    def test_root_and_child_boundaries(self):
        self.assertEqual(storage.root_path(str(self.root)), self.root)
        with self.assertRaises(storage.Error):
            storage.root_path(str(self.root / "missing"))
        with self.assertRaises(storage.Error):
            storage.child(self.root, "..", "escape")
        with self.assertRaises(storage.Error):
            storage.child(self.root, "/absolute")
        self.assertEqual(storage.child(self.root, "missing", "next"), self.root / "missing/next")
        with self.assertRaises(storage.Error):
            storage.child(self.root, "missing", exists=True)

    def test_symlink_and_hardlink_rejected(self):
        target = self.root / "target"
        target.write_text("unchanged", encoding="utf-8")
        alias = self.root / "alias"
        alias.symlink_to(target)
        for action in (lambda: storage.child(self.root, "alias"), lambda: storage.read(alias), lambda: storage.atomic(alias, "bad")):
            with self.assertRaises(storage.Error):
                action()
        hard = self.root / "hard"
        os.link(target, hard)
        for action in (lambda: storage.child(self.root, "hard"), lambda: storage.read(hard), lambda: storage.atomic(hard, "bad")):
            with self.assertRaises(storage.Error):
                action()
        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

    def test_linked_ancestor_is_rejected(self):
        directory = self.root / "real"
        directory.mkdir()
        (directory / "file").write_text("data", encoding="utf-8")
        (self.root / "alias").symlink_to(directory, target_is_directory=True)
        with self.assertRaises(storage.Error):
            storage.child(self.root, "alias", "file")
        with self.assertRaises(storage.Error):
            storage.read(self.root / "alias/file")
        with self.assertRaises(storage.Error):
            storage.atomic(self.root / "alias/new", "data")

    def test_utf8_newlines_limits_and_regular_file_requirement(self):
        path = self.root / "file"
        value = "你好\r\nline\n"
        self.assertEqual(bool(storage.atomic(path, value)), os.name == "nt")
        self.assertEqual(storage.read(path), value)
        self.assertEqual(path.read_bytes(), value.encode())
        self.assertEqual(storage.read(path, len(value.encode())), value)
        with self.assertRaises(storage.Error):
            storage.read(path, len(value.encode()) - 1)
        path.write_bytes(b"\xff")
        with self.assertRaises(storage.Error):
            storage.read(path)
        with self.assertRaises(storage.Error):
            storage.read(self.root)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO")
    def test_fifo_is_rejected_without_blocking(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        for action in (lambda: storage.read(fifo), lambda: storage.atomic(fifo, "bad"), lambda: storage.child(self.root, "fifo")):
            with self.assertRaises(storage.Error):
                action()
        with self.assertRaises(storage.Error):
            with storage.locked(fifo):
                pass

    def test_read_detects_replacement_between_check_and_open(self):
        path = self.root / "file"
        path.write_text("first", encoding="utf-8")
        replacement = self.root / "replacement"
        replacement.write_text("second", encoding="utf-8")
        original = os.open
        fired = False

        def racing_open(name, *args, **kwargs):
            nonlocal fired
            if Path(name) == path and not fired:
                fired = True
                os.replace(replacement, path)
            return original(name, *args, **kwargs)

        with mock.patch.object(storage.os, "open", side_effect=racing_open):
            with self.assertRaises(storage.Error):
                storage.read(path)

    def test_atomic_file_sync_failure_is_precommit(self):
        path = self.root / "CURRENT.md"
        path.write_text("old", encoding="utf-8")
        with mock.patch.object(storage.os, "fsync", side_effect=OSError("injected file fsync failure")):
            with self.assertRaises(storage.Error):
                storage.atomic(path, "new")
        self.assertEqual(path.read_text(encoding="utf-8"), "old")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["CURRENT.md"])

    def test_reparse_attributes_rejected_even_without_symlink_mode(self):
        import stat
        from types import SimpleNamespace
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_file_attributes=0x400)
        with self.assertRaises(storage.Error):
            storage._validate(info)

    @unittest.skipIf(os.name == "nt", "uses POSIX lock interception")
    def test_lock_detects_inode_replacement_after_acquisition(self):
        import fcntl
        path = self.root / "lock"
        path.write_text("", encoding="utf-8")
        replacement = self.root / "replacement"
        replacement.write_text("", encoding="utf-8")
        original = fcntl.flock

        def swapped(fd, operation):
            original(fd, operation)
            if operation == fcntl.LOCK_EX:
                os.replace(replacement, path)

        with mock.patch.object(fcntl, "flock", side_effect=swapped):
            with self.assertRaises(storage.Error):
                with storage.locked(path):
                    self.fail("replaced lock was accepted")

    def test_atomic_precommit_failure_preserves_current_and_cleans_temp(self):
        path = self.root / "CURRENT.md"
        path.write_text("old", encoding="utf-8")
        with mock.patch.object(storage.os, "replace", side_effect=OSError("injected replacement failure")):
            with self.assertRaises(storage.Error):
                storage.atomic(path, "new")
        self.assertEqual(path.read_text(encoding="utf-8"), "old")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["CURRENT.md"])

    def test_atomic_postcommit_parent_sync_failure_is_warning(self):
        path = self.root / "CURRENT.md"
        original = os.fsync
        calls = 0

        def fsync(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected parent sync failure")
            return original(fd)

        with mock.patch.object(storage.os, "fsync", side_effect=fsync):
            warnings = storage.atomic(path, "committed")
        self.assertEqual(path.read_text(encoding="utf-8"), "committed")
        self.assertTrue(warnings)
        self.assertIn("durability", warnings[0])

    def test_commit_snapshots_old_first_and_retries(self):
        current = self.root / "CURRENT.md"
        current.write_text("old\r\n", newline="", encoding="utf-8")
        history = self.root / "history"
        original = storage.atomic

        def fail_current(path, document):
            if path == current:
                raise storage.Error("injected current failure")
            return original(path, document)

        with mock.patch.object(storage, "atomic", side_effect=fail_current):
            with self.assertRaises(storage.Error):
                storage.commit(current, history, "old\r\n", "new", 3)
        self.assertEqual(storage.read(current), "old\r\n")
        name = f"r3-{hashlib.sha256(b'old' + bytes([13, 10])).hexdigest()}.md"
        self.assertEqual(storage.read(history / name), "old\r\n")
        result = storage.commit(current, history, "old\r\n", "new", 3)
        self.assertEqual(storage.read(current), "new")
        self.assertEqual(result["history"], str(history / name))
        self.assertEqual(bool(result["warnings"]), os.name == "nt")
        self.assertEqual(len(list(history.iterdir())), 1)

    def test_history_failure_and_mismatch_leave_current_unchanged(self):
        current = self.root / "CURRENT.md"
        current.write_text("old", encoding="utf-8")
        history = self.root / "history"
        with mock.patch.object(storage, "atomic", side_effect=storage.Error("history failure")):
            with self.assertRaises(storage.Error):
                storage.commit(current, history, "old", "new", 1)
        self.assertEqual(storage.read(current), "old")
        name = f"r1-{hashlib.sha256(b'old').hexdigest()}.md"
        (history / name).write_text("wrong", encoding="utf-8")
        with self.assertRaises(storage.Error):
            storage.commit(current, history, "old", "new", 1)
        self.assertEqual(storage.read(current), "old")

    def test_commit_rejects_stale_old_text(self):
        current = self.root / "CURRENT.md"
        current.write_text("actual", encoding="utf-8")
        with self.assertRaises(storage.Error):
            storage.commit(current, self.root / "history", "stale", "new", 1)
        self.assertEqual(storage.read(current), "actual")

    def test_immutable_receipt_never_overwrites(self):
        path = self.root / "receipt.jsonl"
        self.assertEqual(bool(storage.immutable(path, "first")), os.name == "nt")
        self.assertEqual(storage.immutable(path, "first"), [])
        with self.assertRaises(storage.Error):
            storage.immutable(path, "different")
        self.assertEqual(path.read_text(encoding="utf-8"), "first")

    def test_regular_lock_and_hardlinked_lock(self):
        path = self.root / "lock"
        with storage.locked(path):
            self.assertTrue(path.is_file())
        os.link(path, self.root / "other")
        with self.assertRaises(storage.Error):
            with storage.locked(path):
                pass

    @unittest.skipIf(os.name == "nt", "POSIX child process lock timing")
    def test_lock_serializes_processes(self):
        path = self.root / "lock"
        acquired = self.root / "acquired"
        wrapper = '''import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("s", sys.argv[1])
s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)
pathlib.Path(sys.argv[4]).touch()
with s.locked(pathlib.Path(sys.argv[2])):
    pathlib.Path(sys.argv[3]).touch()
'''
        ready = self.root / "ready"
        import time
        with storage.locked(path):
            process = subprocess.Popen([sys.executable, "-c", wrapper, str(SCRIPT), str(path), str(acquired), str(ready)])
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                self.assertFalse(acquired.exists())
            except BaseException:
                process.terminate()
                process.wait(timeout=5)
                raise
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertTrue(acquired.exists())


if __name__ == "__main__":
    unittest.main()
