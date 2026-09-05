import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import git_state


class GitStateTests(unittest.TestCase):
    def test_stat_and_fstat_precision_are_compared_with_same_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'file').write_bytes(b'content')
            real_fstat = os.fstat
            def different_precision(fd):
                info = real_fstat(fd)
                values = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
                values['st_ctime_ns'] += 100
                return SimpleNamespace(**values)
            with mock.patch.object(os, 'fstat', side_effect=different_precision):
                result = git_state.capture(root)
            self.assertEqual(result['kind'], 'git', result)

    @unittest.skipIf(os.name == 'nt', 'Windows does not expose POSIX executable modes')
    def test_mode_change_is_detected_in_already_dirty_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            def git(*args):
                return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
            git('init', '-q')
            script = root / 'run.sh'
            script.write_text('echo test\n', encoding="utf-8")
            script.chmod(0o644)
            git('add', 'run.sh')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'initial')
            (root / 'untracked').write_text('already dirty', encoding="utf-8")
            before = git_state.capture(root)
            script.chmod(0o755)
            after = git_state.capture(root)
            self.assertTrue(before['dirty'] and after['dirty'])
            self.assertEqual(before['head'], after['head'])
            self.assertNotEqual(before['fingerprint'], after['fingerprint'])

    def test_git_inspection_never_runs_clean_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
            git("init", "-q")
            (root / "file.txt").write_text("before", encoding="utf-8")
            git("add", "file.txt")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
            marker = root / "FILTER_EXECUTED"
            (root / ".gitattributes").write_text("file.txt filter=probe\n", encoding="utf-8")
            git("config", "filter.probe.clean", "echo unsafe > FILTER_EXECUTED; cat")
            (root / "file.txt").write_text("after", encoding="utf-8")
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
            (root / "file.txt").write_text("initial", encoding="utf-8")
            git("add", "file.txt")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
            initial = git_state.capture(root)
            self.assertEqual(initial["kind"], "git", initial)
            self.assertFalse(initial["dirty"])
            (root / "file.txt").write_text("changed", encoding="utf-8")
            changed = git_state.capture(root)
            self.assertEqual(changed["kind"], "git", changed)
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
