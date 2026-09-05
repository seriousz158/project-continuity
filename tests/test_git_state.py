import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import git_state


class GitStateTests(unittest.TestCase):
    def test_git_inspection_never_runs_clean_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
            git("init", "-q")
            (root / "file.txt").write_text("before")
            git("add", "file.txt")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
            marker = root / "FILTER_EXECUTED"
            (root / ".gitattributes").write_text("file.txt filter=probe\n")
            git("config", "filter.probe.clean", "echo unsafe > FILTER_EXECUTED; cat")
            (root / "file.txt").write_text("after")
            git_state.capture(root)
            self.assertFalse(marker.exists(), "read-only inspection executed configured filter")

    def test_no_git_and_unavailable_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            self.assertEqual(git_state.capture(root), {"kind": "none"})
            with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError()):
                self.assertEqual(git_state.capture(root)["kind"], "error")

    def test_commits_detached_and_nested_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
            git("init", "-q")
            (root / "file.txt").write_text("initial")
            git("add", "file.txt")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
            initial = git_state.capture(root)
            self.assertEqual(initial["kind"], "git")
            self.assertFalse(initial["dirty"])
            (root / "file.txt").write_text("changed")
            changed = git_state.capture(root)
            self.assertEqual(initial["head"], changed["head"])
            self.assertNotEqual(initial["fingerprint"], changed["fingerprint"])
            git("checkout", "--detach", "-q")
            self.assertEqual(git_state.capture(root)["branch"], "detached")
            (root / "nested").mkdir()
            self.assertEqual(git_state.capture(root / "nested")["kind"], "error")

    def test_environment_cannot_redirect_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            with mock.patch.dict(os.environ, {"GIT_DIR": str(root / "does-not-exist"), "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "unsafe"}):
                self.assertEqual(git_state.capture(root)["kind"], "git")


if __name__ == "__main__":
    unittest.main()
