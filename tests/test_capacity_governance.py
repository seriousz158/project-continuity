"""Capacity receipts, read-only patch previews and long-record hints.

Every case runs in an isolated temporary project.  No live relay, worktree or
real project repository is read or written.

Contract under test:

  * every successful write reports a uniform, additive ``capacity`` block and
    the pre-existing ``compaction``/``objects`` fields are unchanged;
  * an idempotent replay reports the current occupancy as a replay, never as a
    fresh commit;
  * ``capacity --input`` previews a real typed patch with the commit planner,
    creates no lock/lease/object/history and leaves CURRENT.md byte-identical,
    and (for a fixed clock and identity) predicts the exact commit bytes;
  * another writer's live lease is refused by name instead of being predicted;
  * UTF-8 byte accounting includes non-ASCII text;
  * long-record hints locate a field without echoing its value.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cli_v2 as cli
import progress as p


class _FakeStdin:
    def __init__(self, payload: bytes):
        self.buffer = io.BytesIO(payload)


class CapacityGovernanceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.assertEqual(self.call("init", "--name", "capgov fixture")[0], 0)

    def call(self, *args, patch=None, expect=0, frozen=None):
        argv = list(args)
        if patch is not None:
            argv = argv + ["--input", "-"]
        out, err = io.StringIO(), io.StringIO()
        ctx = (mock.patch.object(cli, "now", return_value=frozen)
               if frozen is not None else contextlib.nullcontext())
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), ctx:
            if patch is not None:
                with mock.patch.object(sys, "stdin",
                                       _FakeStdin(json.dumps(patch).encode("utf-8"))):
                    code = cli.main(argv + ["--root", str(self.root)])
            else:
                code = cli.main(argv + ["--root", str(self.root)])
        if expect is not None:
            self.assertEqual(code, expect, err.getvalue() or out.getvalue())
        body = out.getvalue().strip()
        return code, (json.loads(body) if body else {}), err.getvalue()

    def patch_file(self, patch):
        path = self.root / "change.json"
        path.write_text(json.dumps(patch, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def current_bytes(self):
        return len((self.root / ".relay" / "CURRENT.md").read_bytes())

    def relay_files(self):
        return sorted(str(path.relative_to(self.root))
                      for path in (self.root / ".relay").rglob("*") if path.is_file())


class CapacityReceiptTests(CapacityGovernanceCase):
    def test_write_reports_capacity_and_keeps_compaction_keys(self):
        result = self.call("resume", "--writer", "w", "--expected-revision", "0",
                           "--operation-id", "r1")[1]
        capacity = result["capacity"]
        self.assertTrue(capacity["committed"])
        self.assertFalse(capacity["replayed"])
        self.assertEqual(capacity["used_bytes"], self.current_bytes())
        self.assertEqual(capacity["limit_bytes"], p.MAX_BYTES)
        self.assertEqual(capacity["headroom_bytes"], p.MAX_BYTES - capacity["used_bytes"])
        self.assertEqual(capacity["delta_bytes"],
                         result["compaction"]["after_bytes"] - result["compaction"]["before_bytes"])
        self.assertIn("recommended_action", capacity)
        self.assertEqual(capacity["object_store_new_bytes"], 0)
        # The pre-existing fields are untouched.
        for key in ("before_bytes", "after_bytes", "near_limit", "target_bytes",
                    "applied", "archived_segments", "retained_receipts"):
            self.assertIn(key, result["compaction"])
        self.assertIn("objects", result)
        for key in ("revision", "schema", "writer", "lease_until", "replayed"):
            self.assertIn(key, result)

    def test_delta_bytes_can_be_negative_when_compaction_shrinks_the_document(self):
        receipt = cli._capacity_receipt(cli.p.SCHEMA_V2, 1000, 800, committed=True)
        self.assertEqual(receipt["delta_bytes"], -200)
        self.assertEqual(receipt["used_bytes"], 800)
        self.assertEqual(receipt["headroom_bytes"], p.MAX_BYTES - 800)
        self.assertFalse(receipt["near_limit"])

    def test_utf8_bytes_are_counted(self):
        text = "中文进度 · 状态✅ emoji 🚀 换行\n第二行"
        self.call("resume", "--writer", "w", "--expected-revision", "0",
                  "--operation-id", "r1")
        before = self.current_bytes()
        result = self.call("update", "--writer", "w", "--expected-revision", "1",
                           "--operation-id", "u1", patch={"project": {"next_step": text}})[1]
        self.assertEqual(result["capacity"]["delta_bytes"], self.current_bytes() - before)

    def test_replay_is_not_reported_as_a_new_commit(self):
        self.call("resume", "--writer", "w", "--expected-revision", "0",
                  "--operation-id", "r1")
        self.call("save", "--writer", "w", "--expected-revision", "1",
                  "--operation-id", "s1")
        code, replay, _err = self.call("resume", "--writer", "w", "--expected-revision", "0",
                                       "--operation-id", "r1")
        self.assertEqual(code, 0)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["revision"], 1)
        self.assertEqual(replay["current_revision"], 2)
        self.assertFalse(replay["capacity"]["committed"])
        self.assertTrue(replay["capacity"]["replayed"])
        self.assertEqual(replay["capacity"]["delta_bytes"], 0)
        self.assertEqual(replay["capacity"]["used_bytes"], self.current_bytes())
        self.assertEqual(replay["capacity"]["next_save_cycle_estimate"]["status"],
                         "not_computed")


class PatchPreviewTests(CapacityGovernanceCase):
    def test_preview_is_read_only(self):
        self.call("resume", "--writer", "w", "--expected-revision", "0",
                  "--operation-id", "r1")
        self.call("save", "--writer", "w", "--expected-revision", "1",
                  "--operation-id", "s1")
        before_bytes = (self.root / ".relay" / "CURRENT.md").read_bytes()
        before_files = self.relay_files()
        patch = {"project": {"next_step": "preview only"}}
        result = self.call("capacity", "--writer", "w",
                           "--input", self.patch_file(patch))[1]
        preview = result["patch_preview"]
        self.assertTrue(preview["read_only"])
        self.assertFalse(preview["created_files"])
        self.assertEqual(preview["mode"], "cycle")
        self.assertEqual((self.root / ".relay" / "CURRENT.md").read_bytes(), before_bytes)
        self.assertEqual(self.relay_files(), before_files)

    def test_preview_predicts_the_exact_commit_bytes(self):
        frozen = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
        patch = {"project": {"next_step": "frozen clock"}, "tasks": [
            {"id": "t1", "title": "T", "acceptance": ["a"]}]}
        preview = self.call("capacity", "--writer", "w", "--operation-id", "cycle",
                            "--input", self.patch_file(patch), frozen=frozen)[1]["patch_preview"]
        self.assertTrue(preview["would_fit"])
        resumed = self.call("resume", "--writer", "w", "--expected-revision", "0",
                            "--operation-id", "cycle-resume", frozen=frozen)[1]
        committed = self.call("save", "--writer", "w", "--expected-revision", "1",
                              "--operation-id", "cycle-save", patch=patch, frozen=frozen)[1]
        self.assertEqual(resumed["capacity"]["used_bytes"],
                         preview["steps"][0]["candidate_bytes"])
        self.assertEqual(committed["capacity"]["used_bytes"],
                         preview["final_candidate_bytes"])
        self.assertEqual(committed["compaction"]["after_bytes"],
                         preview["final_candidate_bytes"])

    def test_preview_refuses_another_writer_lease(self):
        self.call("resume", "--writer", "w1", "--expected-revision", "0",
                  "--operation-id", "lease1")
        patch = {"project": {"next_step": "x"}}
        code, _out, err = self.call("capacity", "--writer", "w2",
                                    "--input", self.patch_file(patch), expect=2)
        self.assertIn("RELAY_PREVIEW_LEASE_CONFLICT", err)

    def test_preview_models_the_lease_holder_save(self):
        self.call("resume", "--writer", "w1", "--expected-revision", "0",
                  "--operation-id", "lease1")
        patch = {"project": {"next_step": "holder save"}}
        preview = self.call("capacity", "--writer", "w1",
                            "--input", self.patch_file(patch))[1]["patch_preview"]
        self.assertEqual(preview["mode"], "save")
        self.assertEqual(preview["commands"], ["save"])

    def test_preview_does_not_create_relay_when_absent(self):
        absent = self.root / "no-relay-here"
        absent.mkdir()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["capacity", "--root", str(absent)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertFalse(json.loads(out.getvalue())["exists"])
        self.assertFalse((absent / ".relay").exists())

    def test_preview_reports_next_cycle_advice_when_the_cycle_is_tight(self):
        import copy
        self.call("resume", "--writer", "w", "--expected-revision", "0",
                  "--operation-id", "r0")
        document, lines, body, meta, state, _matches = cli.read_envelope(self.root)
        base_state = copy.deepcopy(state)

        def task(index, pad=40):
            return {"id": f"t{index}", "title": "title-" + "y" * pad, "status": "todo",
                    "owner": None, "depends_on": [], "acceptance": ["cond"],
                    "generation": 0}

        def render(value):
            return cli.metadata(lines, {}) + p.render_body(value, body)

        empty = copy.deepcopy(base_state)
        empty["tasks"] = []
        base = len(render(empty).encode("utf-8"))
        sample = copy.deepcopy(base_state)
        sample["tasks"] = [task(0)]
        per_task = len(render(sample).encode("utf-8")) - base
        count = max(1, (p.MAX_BYTES - 300 - base) // per_task)
        grown = copy.deepcopy(base_state)
        grown["tasks"] = [task(i) for i in range(count)]
        while len(render(grown).encode("utf-8")) > p.MAX_BYTES - 300:
            grown["tasks"].pop()
        (self.root / ".relay" / "CURRENT.md").write_text(render(grown), encoding="utf-8")
        self.assertLessEqual(self.current_bytes(), p.MAX_BYTES)
        preview = self.call("capacity", "--writer", "w",
                            "--input", self.patch_file(
                                {"tasks": [{"id": "extra", "title": "E", "acceptance": ["a"]}]})
                            )[1]["patch_preview"]
        self.assertEqual(preview["mode"], "save")
        self.assertTrue(preview["would_fit"])
        estimate = preview["next_save_cycle_estimate"]
        self.assertEqual(estimate["status"], "modelled")
        self.assertFalse(estimate["would_fit"])
        self.assertIn("next", preview["recommended_action"])


class LongRecordHintTests(CapacityGovernanceCase):
    def test_hint_locates_a_field_without_echoing_its_value(self):
        marker = "SENSITIVE-MARKER-" + "z" * 2000
        self.call("resume", "--writer", "w", "--expected-revision", "0",
                  "--operation-id", "r1")
        self.call("update", "--writer", "w", "--expected-revision", "1",
                  "--operation-id", "u1",
                  patch={"tasks": [{"id": "t1", "title": "T", "acceptance": ["a"]}],
                         "decisions": [{"id": "d1", "task_ids": ["t1"],
                                        "conclusion": "c", "reason": marker}]})
        result = self.call("capacity", "--long-record-threshold", "1000")[1]
        hints = result["long_records"]["hints"]
        self.assertTrue(any(h["collection"] == "decisions" and h["id"] == "d1"
                            and h["field"] == "reason" for h in hints))
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(marker, serialized)

    def test_threshold_is_configurable(self):
        self.call("resume", "--writer", "w", "--expected-revision", "0",
                  "--operation-id", "r1")
        self.call("update", "--writer", "w", "--expected-revision", "1",
                  "--operation-id", "u1",
                  patch={"tasks": [{"id": "t1", "title": "T", "acceptance": ["a"]}]})
        result = self.call("capacity", "--long-record-threshold", "1")[1]
        self.assertEqual(result["long_records"]["threshold_bytes"], 1)
        self.assertGreater(result["long_records"]["count"], 0)


if __name__ == "__main__":
    unittest.main()
