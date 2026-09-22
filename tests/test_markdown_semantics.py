"""Markdown provenance semantics: source_revision is the first externalisation.

The contract under test (no schema change):

  * ``source_revision`` records where the text was FIRST externalised from and
    is never advanced by a later content change;
  * content identity is carried by ``sha256``/``bytes``/``sections``, so a
    consumer that compares those is not misled by an older ``source_revision``;
  * a resume that changes no Markdown leaves the text digest unchanged;
  * the superseded section object stays on disk and the restored text is
    byte-for-byte the new content.

Every case runs in an isolated temporary project.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cli_v2 as cli
import progress as p

REGION = "\n## 目标\n- keep this text\n## 状态\n- 初始正文（中文）\n"


class _FakeStdin:
    def __init__(self, payload: bytes):
        self.buffer = io.BytesIO(payload)


class MarkdownSemanticsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.assertEqual(self.call("init", "--name", "md fixture")[0], 0)
        self.seed_v5()

    def call(self, *args, patch=None, expect=0):
        argv = list(args)
        if patch is not None:
            argv = argv + ["--input", "-"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if patch is not None:
                with mock.patch.object(sys, "stdin",
                                       _FakeStdin(json.dumps(patch).encode("utf-8"))):
                    code = cli.main([*argv, "--root", str(self.root)])
            else:
                code = cli.main([*argv, "--root", str(self.root)])
        if expect is not None:
            self.assertEqual(code, expect, err.getvalue() or out.getvalue())
        body = out.getvalue().strip()
        return code, (json.loads(body) if body else {}), err.getvalue()

    def current(self):
        return (self.root / ".relay" / "CURRENT.md").read_text(encoding="utf-8")

    def write_current(self, text):
        (self.root / ".relay" / "CURRENT.md").write_text(text, encoding="utf-8")

    def set_region(self, region):
        lines, body, _meta = cli.split(self.current())
        self.write_current(cli.metadata(lines, {}) + p.render_region(body, region))

    def seed_v5(self):
        self.call("resume", "--writer", "w", "--expected-revision", "0",
                  "--operation-id", "r1")
        self.call("save", "--writer", "w", "--expected-revision", "1",
                  "--operation-id", "s1",
                  patch={"tasks": [{"id": "t1", "title": "T", "acceptance": ["a"]}],
                         "evidence": [{"id": "ev1", "task_id": "t1", "check": "c",
                                       "result": "pass", "at": "2026-09-21T00:00:00Z",
                                       "ref": "runs/ev1", "acceptance": ["a"]}]})
        preview = self.call("migrate", "--to-v4")[1]
        self.call("migrate", "--to-v4", "--apply", "--writer", "w",
                  "--expected-revision", "2", "--operation-id", "m4",
                  "--source-sha256", preview["source_sha256"])
        self.set_region(REGION)
        preview = self.call("migrate", "--to-v5")[1]
        revision = self.call("status")[1]["revision"]
        self.call("migrate", "--to-v5", "--apply", "--writer", "w",
                  "--expected-revision", str(revision), "--operation-id", "m5",
                  "--source-sha256", preview["source_sha256"])
        self.migration_revision = revision

    def markdown(self):
        return self.call("markdown")[1]

    def test_source_revision_is_first_externalisation_not_last_update(self):
        first = self.markdown()
        self.assertEqual(first["provenance"]["source_revision"], self.migration_revision)
        self.assertEqual(first["text"], REGION)
        meaning = first["provenance_meaning"]
        self.assertIn("FIRST externalised", meaning["source_revision"])
        self.assertIn("sha256", meaning["content_identity"])

        revision = self.call("status")[1]["revision"]
        self.call("resume", "--writer", "w", "--expected-revision", str(revision),
                  "--operation-id", "r2")
        replaced = "## 状态\n- 更新后的正文（中文）\n"
        result = self.call("save", "--writer", "w", "--expected-revision", str(revision + 1),
                           "--operation-id", "s2",
                           patch={"markdown": [{"title": "状态", "content": replaced}]})[1]
        self.assertEqual(result["markdown"]["replaced"], 1)

        second = self.markdown()
        # The provenance keeps the first-externalisation revision...
        self.assertEqual(second["provenance"]["source_revision"], self.migration_revision)
        # ...while the content identity changed.
        self.assertNotEqual(second["sha256"], first["sha256"])
        self.assertNotEqual(second["bytes"], first["bytes"])
        self.assertIn(replaced, second["text"])
        self.assertEqual(second["sha256"],
                         hashlib.sha256(second["text"].encode("utf-8")).hexdigest())

    def test_resume_does_not_change_the_text_digest(self):
        before = self.markdown()
        revision = self.call("status")[1]["revision"]
        self.call("resume", "--writer", "w", "--expected-revision", str(revision),
                  "--operation-id", "r2")
        after = self.markdown()
        self.assertEqual(after["sha256"], before["sha256"])
        self.assertEqual(after["bytes"], before["bytes"])
        self.assertEqual(after["count"], before["count"])
        self.assertEqual(after["provenance"]["source_revision"],
                         before["provenance"]["source_revision"])

    def test_superseded_section_object_stays_on_disk(self):
        _document, _lines, _body, meta, _stub, _matches = cli.read_envelope(self.root)
        revision = int(meta["revision"])
        objects = self.root / ".relay" / "objects" / "markdown"
        before = {path.name for path in objects.rglob("*.json")}
        self.assertTrue(before)
        self.call("resume", "--writer", "w", "--expected-revision", str(revision),
                  "--operation-id", "r2")
        self.call("save", "--writer", "w", "--expected-revision", str(revision + 1),
                  "--operation-id", "s2",
                  patch={"markdown": [{"title": "状态", "content": "## 状态\n- new\n"}]})
        after = {path.name for path in objects.rglob("*.json")}
        # Content-addressed objects are never overwritten or removed: the
        # superseded section object remains for verification and recovery.
        self.assertTrue(before <= after, "a previously published object was removed")


if __name__ == "__main__":
    unittest.main()
