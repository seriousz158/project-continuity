"""Independent logical-equivalence checker for migrations."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cli_v2 as cli
import migrate_compare
import progress as p


class _FakeStdin:
    def __init__(self, payload):
        self.buffer = io.BytesIO(payload)


class CompareCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.assertEqual(self.call("init", "--name", "compare fixture")[0], 0)

    def call(self, *args, patch=None):
        argv = list(args)
        if patch is not None:
            argv = argv + ["--input", "-"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if patch is not None:
                with mock.patch.object(sys, "stdin",
                                       _FakeStdin(json.dumps(patch).encode("utf-8"))):
                    code = cli.main(argv + ["--root", str(self.root)])
            else:
                code = cli.main(argv + ["--root", str(self.root)])
        return code, out.getvalue().strip(), err.getvalue()

    def document(self):
        return (self.root / ".relay" / "CURRENT.md").read_text(encoding="utf-8")

    def seed(self):
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision", "0",
                                   "--operation-id", "r")[0], 0)
        patch = {"tasks": [{"id": "t1", "title": "fixture", "status": "doing",
                            "acceptance": ["one", "two"]}],
                 "evidence": [{"id": "ev-1", "task_id": "t1", "check": "c",
                               "result": "pass", "at": "2026-09-16T00:00:00Z",
                               "ref": "runs/one", "acceptance": ["one"]}]}
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision", "1",
                                   "--operation-id", "s", patch=patch)[0], 0)

    def to_v4(self):
        preview = json.loads(self.call("migrate", "--to-v4")[1])
        revision = cli.read_envelope(self.root)[3]["revision"]
        self.assertEqual(self.call("migrate", "--to-v4", "--apply", "--writer", "w",
                                   "--expected-revision", revision,
                                   "--operation-id", "m",
                                   "--source-sha256", preview["source_sha256"])[0], 0)


class CompareTests(CompareCase):
    def test_migration_is_equivalent_and_round_trips(self):
        self.seed()
        before = self.document()
        self.to_v4()
        equal, report = migrate_compare.compare(before, self.document(), self.root)
        self.assertTrue(equal, json.dumps(report, ensure_ascii=False))
        self.assertEqual(report["collections"]["evidence"]["before"], 1)
        self.assertEqual(report["collections"]["evidence"]["after"], 1)
        self.assertEqual(report["collections"]["tasks"]["changed"], [])
        self.assertEqual(report["project"], "identical")
        self.assertEqual(report["extensions"], "identical")
        self.assertEqual(report["operations"]["missing"], [])
        self.assertTrue(report["roundtrip"]["matches"])
        self.assertTrue(report["roundtrip"]["applicable"])

    def test_changed_business_record_is_reported(self):
        self.seed()
        before = self.document()
        lines, body, meta = cli.split(before)
        state = cli.read_envelope(self.root)[4]
        state["tasks"][0]["title"] = "renamed"
        changed = cli.metadata(lines, {}) + p.render_body(state, body)
        equal, report = migrate_compare.compare(before, changed)
        self.assertFalse(equal)
        self.assertEqual(report["collections"]["tasks"]["changed"], ["t1"])

    def test_missing_and_extra_records_are_reported(self):
        self.seed()
        before = self.document()
        lines, body, meta = cli.split(before)
        state = cli.read_envelope(self.root)[4]
        state["evidence"] = []
        trimmed = cli.metadata(lines, {}) + p.render_body(state, body)
        equal, report = migrate_compare.compare(before, trimmed)
        self.assertFalse(equal)
        self.assertEqual(report["collections"]["evidence"]["missing"], ["ev-1"])

    def test_v4_without_root_is_refused(self):
        self.seed()
        before = self.document()
        self.to_v4()
        after = self.document()
        with self.assertRaises(ValueError):
            migrate_compare.logical_state(after, None)
        before_file = self.root / "before.md"
        after_file = self.root / "after.md"
        before_file.write_text(before, encoding="utf-8")
        after_file.write_text(after, encoding="utf-8")
        # The tool prints its refusal as JSON on stdout; capture it so the
        # aggregate suite output is not polluted by an expected error line.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = migrate_compare.main(
                ["--before", str(before_file), "--after", str(after_file)])
        self.assertEqual(code, 2)
        self.assertIn("explicit project root", out.getvalue())


if __name__ == "__main__":
    unittest.main()
