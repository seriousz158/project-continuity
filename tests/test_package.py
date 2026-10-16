from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import package_skill


class PackageTests(unittest.TestCase):
    def test_skill_is_small_and_preserves_all_command_routes(self):
        import cli_v2
        import argparse
        skill = (ROOT / "SKILL.md").read_bytes()
        self.assertLessEqual(len(skill), 5120)
        routes = next(action for action in cli_v2.parser()._actions
                      if isinstance(action, argparse._SubParsersAction)).choices
        for command in routes:
            self.assertIn("`" + command + "`", skill.decode("utf-8"))

    def test_existing_relay_discovery_is_read_only(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        description = skill.split("---", 2)[1]
        self.assertIn("or the project already contains `.relay/CURRENT.md`", description)
        self.assertIn("status --brief", skill)
        self.assertIn("Never initialize implicitly", skill)

    def test_archive_is_allowlisted_reproducible_and_runnable(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            first = tmp / "first.zip"
            second = tmp / "second.zip"
            _, sidecar, digest = package_skill.package(first, ROOT)
            package_skill.package(second, ROOT)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), digest)
            self.assertEqual(sidecar.read_text(encoding="ascii"), f"{digest}  first.zip\n")
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
                expected = [f"{package_skill.ARCHIVE_ROOT}/{name}" for name in sorted(package_skill.FILES)]
                self.assertEqual(names, expected)
                self.assertFalse(any("__pycache__" in name or "/.git/" in name or "/.relay/" in name for name in names))
                archive.extractall(tmp / "unpacked")
            extracted = tmp / "unpacked" / package_skill.ARCHIVE_ROOT
            self.assertFalse(any(path.name == "__pycache__" for path in extracted.rglob("*")))
            cli = extracted / "scripts" / "write_current.py"
            project = tmp / "fresh-project"
            project.mkdir()
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

            def run(*args, input_text=None):
                result = subprocess.run(
                    [sys.executable, str(cli), *map(str, args)], cwd=extracted,
                    input=input_text, capture_output=True, text=True, env=env,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            run("init", "--schema", "v2", "--root", project)
            self.assertEqual(run("status", "--root", project)["revision"], 0)
            run("resume", "--root", project, "--writer", "package-test",
                "--expected-revision", "0", "--operation-id", "resume-1")
            run("save", "--root", project, "--writer", "package-test",
                "--expected-revision", "1", "--operation-id", "save-1",
                "--input", "-", input_text="{}")
            status = run("status", "--root", project)
            self.assertEqual(status["revision"], 2)
            self.assertIsNone(status["writer"])
            for test_file in sorted(
                Path(name).name for name in package_skill.FILES
                if name.startswith("tests/test_") and name != "tests/test_package.py"
            ):
                tests = subprocess.run(
                    [sys.executable, "-m", "unittest", "discover", "-s", "tests",
                     "-p", test_file, "-v"],
                    cwd=extracted, capture_output=True, text=True, env=env,
                )
                self.assertEqual(tests.returncode, 0, tests.stdout + tests.stderr)
            self.assertFalse(any(path.name == "__pycache__" for path in extracted.rglob("*")))
            self.assertNotIn(str(ROOT), "\n".join(
                (extracted / name).read_text(encoding="utf-8")
                for name in package_skill.FILES
            ))

    def test_v060_version_matrix_and_allowlist(self):
        self.assertEqual(package_skill.VERSION, "0.6.0")
        self.assertIn("tests/test_v060.py", package_skill.FILES)
        ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn('"3.14"', ci)
        self.assertIn("python -m pytest", ci)
        self.assertIn("v0.6.0.zip", ci)
        self.assertIn("all twelve OS/Python combinations", (ROOT / "README.md").read_text(encoding="utf-8"))
        for name in ("README.md", "README.zh-CN.md", "CHANGELOG.md"):
            self.assertIn("0.6.0", (ROOT / name).read_text(encoding="utf-8"))

    def test_refuses_existing_destination_or_sidecar(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            existing = tmp / "existing.zip"
            existing.write_bytes(b"keep")
            with self.assertRaises(FileExistsError):
                package_skill.package(existing, ROOT)
            self.assertEqual(existing.read_bytes(), b"keep")
            target = tmp / "new.zip"
            target.with_name("new.zip.sha256").write_text("keep", encoding="ascii")
            with self.assertRaises(FileExistsError):
                package_skill.package(target, ROOT)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
