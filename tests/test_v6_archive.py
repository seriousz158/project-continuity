"""v6: lossless cold archive, schema-aware receipts and the migration chain."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cli_v2 as cli
import progress as p

V1 = '''---
schema: project-continuity/v1
project_id: legacy-test
revision: 7
updated_at: 2026-01-01T00:00:00Z
writer: null
lease_until: null
branch: null
base_commit: null
status: blocked
---
## 目标
- Keep existing text.
'''


class _FakeStdin:
    def __init__(self, payload: bytes):
        self.buffer = io.BytesIO(payload)


def big_patch():
    """Enough cold history to cross the v6 trigger, with closure edge cases."""
    long = "x" * 1500
    stamp = "2026-10-11T00:00:00Z"
    tasks = [{"id": f"done{i}", "title": f"finished {i}", "status": "done",
              "acceptance": [f"cond {i}"]} for i in range(6)]
    tasks += [{"id": "dep", "title": "needed by live", "status": "done",
               "acceptance": ["dep ok"]},
              {"id": "scoped", "title": "named by an open blocker", "status": "cancelled",
               "reason": "superseded", "acceptance": ["scoped ok"]},
              {"id": "live", "title": "current work", "status": "doing",
               "depends_on": ["dep"], "acceptance": ["live ok"]}]
    evidence = [{"id": f"e{i}", "task_id": f"done{i}", "check": "unit", "result": "pass",
                 "at": stamp, "ref": "r", "acceptance": [f"cond {i}"]} for i in range(6)]
    evidence += [{"id": "edep", "task_id": "dep", "check": "unit", "result": "pass",
                  "at": stamp, "ref": "r", "acceptance": ["dep ok"]},
                 {"id": "escoped", "task_id": "scoped", "check": "unit", "result": "pass",
                  "at": stamp, "ref": "r", "acceptance": ["scoped ok"]}]
    blockers = [{"id": f"b{i}", "task_id": "live", "description": long,
                 "status": "resolved", "resolution": "fixed"} for i in range(12)]
    blockers.append({"id": "bopen", "task_id": "live", "description": "still open",
                     "status": "open"})
    decisions = [{"id": f"d{i}", "task_ids": [f"done{i}"], "conclusion": "c" * 300,
                  "reason": "r"} for i in range(6)]
    decisions.append({"id": "dmix", "task_ids": ["done0", "live"], "conclusion": "mixed",
                      "reason": "r"})
    return {"project": {"current_task": "live"}, "tasks": tasks, "evidence": evidence,
            "blockers": blockers, "decisions": decisions,
            "extensions": {"blocker_scope": {"schema": p.BLOCKER_SCOPE_SCHEMA, "entries": [
                {"blocker_id": "bopen", "scope": ["task:live", "task:scoped"]}]}}}


class V6Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

    def call(self, *args, patch=None, expect=0, frozen=None):
        argv = list(args) + (["--input", "-"] if patch is not None else [])
        out, err = io.StringIO(), io.StringIO()
        ctx = (mock.patch.object(cli, "now", return_value=frozen)
               if frozen is not None else contextlib.nullcontext())
        stdin = (mock.patch.object(sys, "stdin", _FakeStdin(json.dumps(patch).encode("utf-8")))
                 if patch is not None else contextlib.nullcontext())
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), ctx, stdin:
            code = cli.main(argv + ["--root", str(self.root)])
        if expect is not None:
            self.assertEqual(code, expect, err.getvalue() or out.getvalue())
        body = out.getvalue().strip()
        return json.loads(body) if body else json.loads(err.getvalue() or "{}")

    def leased(self, revision=0, op="r1", **kw):
        return self.call("resume", "--writer", "a", "--expected-revision", str(revision),
                         "--operation-id", op, **kw)


class V6InitTests(V6Case):
    def test_init_defaults_to_v6_and_v2_stays_available(self):
        self.assertEqual(self.call("init")["schema"], p.SCHEMA_V6)
        status = self.call("status")
        self.assertEqual(status["schema"], p.SCHEMA_V6)
        self.assertEqual(status["archive"]["count"], 0)
        self.assertTrue(status["write_ready"])
        with tempfile.TemporaryDirectory() as raw:
            self.root, saved = Path(raw).resolve(), self.root
            self.assertEqual(self.call("init", "--schema", "v2")["schema"], p.SCHEMA_V2)
            self.root = saved

    def test_small_writes_never_archive(self):
        self.call("init")
        self.leased()
        result = self.call("save", "--writer", "a", "--expected-revision", "1",
                           "--operation-id", "s1", patch={
                               "tasks": [{"id": "t1", "title": "x", "status": "cancelled",
                                          "reason": "dropped", "acceptance": ["a"]}],
                               "blockers": [{"id": "b1", "task_id": "t1", "description": "d",
                                             "status": "resolved", "resolution": "ok"}]})
        self.assertEqual(result["archive"]["archived"], 0)
        self.assertEqual(len(self.call("handoff")["tasks"]), 1)


class V6ArchiveTests(V6Case):
    def setUp(self):
        super().setUp()
        self.call("init")
        self.leased()
        self.result = self.call("update", "--writer", "a", "--expected-revision", "1",
                                "--operation-id", "u1", patch=big_patch())

    def test_cold_records_move_out_above_the_trigger(self):
        _limit, trigger, _target = p.budget(p.SCHEMA_V6)
        self.assertGreater(self.result["archive"]["before_bytes"], trigger)
        self.assertLess(self.result["capacity"]["used_bytes"], trigger)
        self.assertEqual(self.result["archive"]["automatic"], self.result["archive"]["count"])
        handoff = self.call("handoff")
        # done0 shares a decision with the live task, dep is a dependency of the
        # live task, scoped is named by an open blocker: all three stay hot.
        self.assertEqual(sorted(task["id"] for task in handoff["tasks"]),
                         ["dep", "done0", "live", "scoped"])
        self.assertEqual([item["id"] for item in handoff["blockers"]], ["bopen"])
        listing = self.call("archive", "--limit", "500")
        kinds = {}
        for item in listing["items"]:
            kinds.setdefault(item["collection"], set()).add(item["record_id"])
        self.assertEqual(kinds["tasks"], {f"done{i}" for i in range(1, 6)})
        self.assertEqual(kinds["evidence"], {f"e{i}" for i in range(1, 6)})
        self.assertEqual(kinds["decisions"], {f"d{i}" for i in range(1, 6)})
        self.assertEqual(kinds["blockers"], {f"b{i}" for i in range(12)})

    def test_archive_is_lossless_and_deep_checked(self):
        original = next(task for task in big_patch()["tasks"] if task["id"] == "done3")
        record = self.call("archive", "--id", "done3")["records"][0]
        for key, value in original.items():
            self.assertEqual(record["record"][key], value)
        verified = self.call("handoff", "--verify-integrity")
        self.assertEqual(verified["checks"]["integrity"]["state"], "verified")
        self.assertEqual(verified["archive"]["count"], self.result["archive"]["count"])
        # Tampering with an archived object is named corruption, never a pass.
        objects = sorted((self.root / ".relay/objects/archive").rglob("*.json"))
        objects[0].write_bytes(objects[0].read_bytes() + b" ")
        degraded = self.call("handoff", "--verify-integrity")
        self.assertEqual(degraded["checks"]["integrity"]["state"], "degraded")

    def test_extension_null_archives_but_managed_keys_are_refused(self):
        self.call("update", "--writer", "a", "--expected-revision", "2", "--operation-id", "u2",
                  patch={"extensions": {"r1012": {"round": 1012}}})
        result = self.call("update", "--writer", "a", "--expected-revision", "3",
                           "--operation-id", "u3", patch={"extensions": {"r1012": None}})
        self.assertEqual(result["archive"]["explicit"], 1)
        self.assertEqual(self.call("archive", "--id", "r1012")["records"][0]["record"],
                         {"key": "r1012", "value": {"round": 1012}})
        refused = self.call("update", "--writer", "a", "--expected-revision", "4",
                            "--operation-id", "u4", expect=2,
                            patch={"extensions": {"blocker_scope": None}})
        self.assertIn("managed extension", refused["error"])

    def test_export_bundle_carries_the_archive(self):
        bundle = self.root / "handoff.zip"
        self.call("export", "--output", str(bundle))
        self.assertTrue(self.call("verify", "--bundle", str(bundle))["valid"])
        import zipfile
        with zipfile.ZipFile(bundle) as archive:
            self.assertTrue(any(name.startswith("objects/archive/") for name in archive.namelist()))


class V6PreviewTests(V6Case):
    def test_preview_predicts_a_commit_that_archives(self):
        frozen = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
        self.call("init", frozen=frozen)
        self.leased(frozen=frozen)
        path = self.root / "change.json"
        path.write_text(json.dumps(big_patch()), encoding="utf-8", newline="\n")
        preview = self.call("capacity", "--writer", "a", "--operation-id", "s1",
                            "--input", str(path), frozen=frozen)["patch_preview"]
        committed = self.call("save", "--writer", "a", "--expected-revision", "1",
                              "--operation-id", "s1", patch=big_patch(), frozen=frozen)
        self.assertGreater(committed["archive"]["automatic"], 0)
        self.assertEqual(committed["capacity"]["used_bytes"], preview["final_candidate_bytes"])

    def read_only_preview(self, *args, patch, frozen):
        files = sorted((self.root / ".relay").rglob("*"))
        preview = self.call("capacity", *args, patch=patch, frozen=frozen)["patch_preview"]
        self.assertEqual(sorted((self.root / ".relay").rglob("*")), files)
        return preview

    def assert_cycle_matches_commits(self, frozen, patch):
        """An unleased preview predicts its resume and its save byte for byte."""
        revision = self.call("status")["revision"]
        preview = self.read_only_preview("--writer", "b", "--expected-revision", str(revision),
                                         patch=patch, frozen=frozen)
        resumed = self.call("resume", "--writer", "b", "--expected-revision", str(revision),
                            frozen=frozen)
        saved = self.call("save", "--writer", "b", "--expected-revision", str(revision + 1),
                          patch=patch, frozen=frozen)
        self.assertEqual(preview["commands"], ["resume", "save"])
        self.assertEqual(preview["model"]["operation_ids"],
                         [resumed["operation_id"], saved["operation_id"]])
        self.assertEqual([step["candidate_bytes"] for step in preview["steps"]],
                         [resumed["capacity"]["used_bytes"], saved["capacity"]["used_bytes"]])
        return resumed

    def test_unleased_preview_resume_keeps_the_external_markdown(self):
        frozen = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
        self.call("init", frozen=frozen)
        self.leased(frozen=frozen)
        # Sixteen tasks of six conditions: an inline acceptance map alone would
        # push the modelled resume past the trigger.
        tasks = [{"id": f"t{i}", "title": f"task {i}",
                  "acceptance": [f"condition {i}.{j}" for j in range(6)]} for i in range(16)]
        tasks.append({"id": "old", "title": "finished", "status": "done", "acceptance": ["ok"]})
        self.call("save", "--writer", "a", "--expected-revision", "1", "--operation-id", "s1",
                  frozen=frozen, patch={
                      "project": {"current_task": "t0"}, "tasks": tasks,
                      "evidence": [{"id": "e1", "task_id": "old", "check": "unit",
                                    "result": "pass", "at": "2026-10-28T00:00:00Z",
                                    "ref": "r", "acceptance": ["ok"]}]})
        resumed = self.assert_cycle_matches_commits(frozen, {"project": {"next_step": "x"}})
        self.assertEqual(resumed["archive"]["automatic"], 0)

    def test_unleased_preview_save_reads_what_its_resume_archived(self):
        frozen = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
        self.call("init", frozen=frozen)
        self.leased(frozen=frozen)
        # Saved above the trigger without archiving, so the next resume archives.
        self.call("save", "--writer", "a", "--expected-revision", "1", "--operation-id", "s1",
                  "--no-auto-compact", patch=big_patch(), frozen=frozen)
        resumed = self.assert_cycle_matches_commits(frozen, {"project": {"next_step": "x"}})
        self.assertGreater(resumed["archive"]["automatic"], 0)

    def seed_prunable(self, frozen):
        self.call("init", frozen=frozen)
        self.leased(frozen=frozen)
        self.call("save", "--writer", "a", "--expected-revision", "1", "--operation-id", "s1",
                  frozen=frozen, patch={
                      "project": {"current_task": "live"},
                      "tasks": [{"id": "old", "title": "superseded", "acceptance": ["ok"]},
                                {"id": "live", "title": "current", "acceptance": ["ok"]}],
                      "evidence": [{"id": "e-" + key, "task_id": key, "check": "unit",
                                    "result": "pass", "at": "2026-10-28T00:00:00Z", "ref": "r",
                                    "acceptance": ["ok"]} for key in ("old", "live")]})

    @staticmethod
    def prune_patch(revision):
        return {"prune": {"task_ids": ["old"], "blocker_ids": [], "evidence_ids": ["e-old"],
                          "decision_ids": [], "reason": "superseded history",
                          "target_revision": revision},
                "project": {"next_step": "continue"}}

    def test_unleased_prune_preview_saves_from_the_pruned_evidence(self):
        frozen = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
        self.seed_prunable(frozen)
        preview = self.read_only_preview("--writer", "b", "--operation-id", "p",
                                         patch=self.prune_patch(2), frozen=frozen)
        resumed = self.call("resume", "--writer", "b", "--expected-revision", "2",
                            "--operation-id", "p-resume", frozen=frozen)
        pruned = self.call("prune", "--apply", "--writer", "b", "--expected-revision", "3",
                           "--operation-id", "p-prune", patch=self.prune_patch(3), frozen=frozen)
        saved = self.call("save", "--writer", "b", "--expected-revision", "4",
                          "--operation-id", "p-save", frozen=frozen)
        self.assertEqual(preview["commands"], ["resume", "update", "save"])
        self.assertEqual([step["candidate_bytes"] for step in preview["steps"]],
                         [resumed["capacity"]["used_bytes"], pruned["capacity"]["used_bytes"],
                          saved["capacity"]["used_bytes"]])

    def test_leased_prune_preview_saves_from_the_pruned_evidence(self):
        frozen = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
        self.seed_prunable(frozen)
        self.leased(2, op="r2", frozen=frozen)
        preview = self.read_only_preview("--writer", "a", "--operation-id", "p",
                                         patch=self.prune_patch(3), frozen=frozen)
        pruned = self.call("prune", "--apply", "--writer", "a", "--expected-revision", "3",
                           "--operation-id", "p", patch=self.prune_patch(3), frozen=frozen)
        saved = self.call("save", "--writer", "a", "--expected-revision", "4",
                          "--operation-id", "p-save", frozen=frozen)
        self.assertEqual(preview["commands"], ["update", "save"])
        self.assertEqual([step["candidate_bytes"] for step in preview["steps"]],
                         [pruned["capacity"]["used_bytes"], saved["capacity"]["used_bytes"]])


class V6ReceiptTests(V6Case):
    def test_v6_keeps_sixteen_receipts_inline(self):
        self.call("init")
        self.leased()
        for revision in range(1, 40):
            self.call("update", "--writer", "a", "--expected-revision", str(revision),
                      "--operation-id", f"u{revision}")
        result = self.call("compact", "--apply", "--writer", "a", "--expected-revision", "40",
                           "--operation-id", "c1")
        self.assertEqual(result["compaction"]["retained_receipts"], p.RECEIPT_KEEP_V6)
        replay = self.call("update", "--writer", "a", "--expected-revision", "2",
                           "--operation-id", "u2")
        self.assertTrue(replay["replayed"])


class V6MigrationTests(V6Case):
    def write_v2_with_history(self):
        self.call("init", "--schema", "v2")
        self.leased()
        self.call("save", "--writer", "a", "--expected-revision", "1", "--operation-id", "s1",
                  patch={"tasks": [{"id": "t1", "title": "x", "acceptance": ["a"]}],
                         "blockers": [{"id": "b1", "task_id": "t1", "description": "d",
                                       "status": "resolved", "resolution": "ok"}]})

    def test_to_latest_preview_lists_the_chain_and_writes_nothing(self):
        self.write_v2_with_history()
        before = (self.root / ".relay/CURRENT.md").read_bytes()
        preview = self.call("migrate", "--to-latest")
        self.assertEqual([step["to_schema"] for step in preview["steps"]],
                         [p.SCHEMA_V4, p.SCHEMA_V5, p.SCHEMA_V6])
        self.assertEqual(preview["first_step"]["to_schema"], p.SCHEMA_V4)
        self.assertEqual((self.root / ".relay/CURRENT.md").read_bytes(), before)

    def test_to_latest_migrates_v2_step_by_step_without_losing_records(self):
        self.write_v2_with_history()
        before = self.call("handoff")
        preview = self.call("migrate", "--to-latest")
        result = self.call("migrate", "--to-latest", "--apply", "--writer", "a",
                           "--expected-revision", "2", "--operation-id", "m1",
                           "--source-sha256", preview["source_sha256"])
        self.assertEqual(result["schema"], p.SCHEMA_V6)
        self.assertEqual([step["to_schema"] for step in result["steps"]],
                         [p.SCHEMA_V4, p.SCHEMA_V5, p.SCHEMA_V6])
        after = self.call("handoff")
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["blockers"], before["blockers"])
        self.assertEqual(self.call("migrate", "--to-latest")["migration_required"], False)

    def test_migration_with_automatic_archive_is_logically_equivalent(self):
        import migrate_compare
        self.call("init", "--schema", "v2")
        self.leased()
        self.call("save", "--writer", "a", "--expected-revision", "1", "--operation-id", "s1",
                  patch=big_patch())
        before = (self.root / ".relay/CURRENT.md").read_text(encoding="utf-8")
        preview = self.call("migrate", "--to-latest")
        result = self.call("migrate", "--to-latest", "--apply", "--writer", "a",
                           "--expected-revision", "2", "--operation-id", "m1",
                           "--source-sha256", preview["source_sha256"])
        self.assertGreater(result["steps"][-1]["archive"]["automatic"], 0)
        after = (self.root / ".relay/CURRENT.md").read_text(encoding="utf-8")
        equal, report = migrate_compare.compare(before, after, self.root)
        self.assertTrue(equal, report)

    def test_to_latest_requires_the_previewed_source(self):
        self.write_v2_with_history()
        refused = self.call("migrate", "--to-latest", "--apply", "--writer", "a",
                            "--expected-revision", "2", "--operation-id", "m1",
                            "--source-sha256", "0" * 64, expect=2)
        self.assertIn("source hash", refused["error"])
        self.assertEqual(self.call("status")["schema"], p.SCHEMA_V2)

    def test_v1_with_empty_lease_fields_is_readable_and_errors_name_fields(self):
        relay = self.root / ".relay"
        relay.mkdir()
        legacy = V1.replace("writer: null\nlease_until: null", "writer:\nlease_until:")
        (relay / "CURRENT.md").write_text(legacy, encoding="utf-8", newline="\n")
        self.assertEqual(self.call("status")["schema"], "project-continuity/v1")
        (relay / "CURRENT.md").write_text(V1.replace("project_id: legacy-test",
                                                     "project_id: bad id"), encoding="utf-8",
                                       newline="\n")
        self.assertIn("project_id", self.call("status", expect=2)["error"])

    def test_to_latest_converts_v1_first(self):
        relay = self.root / ".relay"
        relay.mkdir()
        (relay / "CURRENT.md").write_text(V1, encoding="utf-8", newline="\n")
        preview = self.call("migrate", "--to-latest")
        self.assertEqual(preview["steps"][0]["to_schema"], p.SCHEMA_V2)
        result = self.call("migrate", "--to-latest", "--apply", "--writer", "a",
                           "--expected-revision", "7", "--operation-id", "m1",
                           "--source-sha256", preview["source_sha256"], "--allow-drift")
        self.assertEqual(result["schema"], p.SCHEMA_V6)
        self.assertIn("Keep existing text.", self.call("markdown")["text"])


if __name__ == "__main__":
    unittest.main()
