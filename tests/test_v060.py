"""v0.6 agent ergonomics, metadata-only diagnostics and read-only governance."""
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


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.call("init")

    def call(self, *args, patch=None, expected=0):
        out, err = io.StringIO(), io.StringIO()
        stdin = mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(patch).encode()))) if patch is not None else contextlib.nullcontext()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), stdin:
            try:
                code = cli.main([*args, "--root", str(self.root), *(["--input", "-"] if patch is not None else [])])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, expected, err.getvalue() or out.getvalue())
        raw = out.getvalue() if out.getvalue() else err.getvalue()
        return json.loads(raw) if raw.startswith("{") else {"error": raw}

    def write(self, command="resume", revision=0, writer="a", op=None, patch=None, extra=(), expected=0):
        return self.call(command, "--writer", writer, "--expected-revision", str(revision),
                         *(["--operation-id", op] if op else []), *extra,
                         patch=patch, expected=expected)


class DiagnosticsTests(Case):
    def test_change_fields_list_keys_not_values(self):
        self.write(op="r")
        result = self.write("update", 1, op="bad", patch={"next_step": "PRIVATE-PAYLOAD"}, expected=2)
        self.assertIn("unexpected [next_step]", result["error"])
        self.assertIn("allowed [", result["error"])
        self.assertNotIn("PRIVATE-PAYLOAD", str(result))

    def test_revision_conflict_names_current_revision(self):
        self.write(op="r")
        result = self.write("update", 0, op="bad", expected=2)
        self.assertIn("current_revision=1", result["error"])
        result = self.call("gc", "--apply", "--writer", "a", "--expected-revision", "0", expected=2)
        self.assertIn("current_revision=1", result["error"])

    def test_lease_conflict_names_holder_and_deadline(self):
        lease = self.write(op="r")
        for command in ("resume", "update"):
            result = self.write(command, 1, writer="b", op=command, expected=2)
            self.assertIn("writer=a", result["error"])
            self.assertIn("lease_until=" + lease["lease_until"], result["error"])

    def test_acceptance_error_lists_ac_id_and_index_not_prose(self):
        self.write(op="r")
        self.write("update", 1, op="task", patch={"tasks": [{"id": "t", "title": "x", "acceptance": ["PRIVATE-CONDITION"]}]})
        result = self.write("update", 2, op="bad", patch={"evidence": [{"id": "e", "task_id": "t", "check": "unit", "result": "pass", "at": "2026-10-16T00:00:00Z", "ref": "report", "acceptance": ["WRONG-CONDITION"]}]}, expected=2)
        self.assertIn(cli.p.ac_identifier("t", "PRIVATE-CONDITION"), result["error"])
        self.assertIn("index=0", result["error"])
        self.assertNotIn("PRIVATE-CONDITION", str(result))
        self.assertNotIn("WRONG-CONDITION", str(result))


class BriefTests(Case):
    def test_brief_is_bounded_projection_and_read_only(self):
        self.write(op="r")
        self.write("save", 1, op="s", patch={"project": {"current_task": "t"}, "tasks": [{"id": "t", "title": "long" * 500, "acceptance": ["ok", "more"]}], "evidence": [{"id": "e", "task_id": "t", "check": "unit", "result": "pass", "at": "2026-10-16T00:00:00Z", "ref": "r", "acceptance": ["ok"]}]})
        before = (self.root / ".relay/CURRENT.md").read_bytes()
        full = self.call("status")
        brief = self.call("status", "--brief")
        for key in ("revision", "schema", "status", "writer", "lease_until", "lease_active", "drift", "git_check_status", "verification", "write_ready", "progress_summary"):
            self.assertEqual(brief[key], full[key])
        summary = brief["progress_summary"]
        self.assertEqual(summary["current_task"], {"id": "t", "status": "todo"})
        self.assertEqual(summary["current_task_uncovered"], 1)
        self.assertEqual(summary["gap_count"], 1)
        self.assertEqual(summary["recent_evidence"], [{"id": "e", "result": "pass"}])
        self.assertLessEqual(len(json.dumps(brief, ensure_ascii=False).encode()), 3072)
        self.assertEqual((self.root / ".relay/CURRENT.md").read_bytes(), before)
        # Timestamps have offsets: lexical order is not chronological order.
        self.write(revision=2, op="r-time")
        times = ["2026-10-17T10:00:00+10:00", "2026-10-17T01:00:00Z",
                 "2026-10-17T02:00:00Z", "2026-10-17T03:00:00Z"]
        self.write("save", 3, op="times", patch={"evidence": [
            {"id": "time" + str(i), "task_id": "t", "check": "unit", "result": "not_run",
             "at": at, "ref": "r", "acceptance": []} for i, at in enumerate(times)]})
        recent = self.call("status", "--brief")["progress_summary"]["recent_evidence"]
        self.assertEqual([e["id"] for e in recent], ["time1", "time2", "time3"])

    def test_brief_without_relay_does_not_initialize(self):
        with tempfile.TemporaryDirectory() as raw:
            self.root = Path(raw).resolve()
            self.assertFalse(self.call("status", "--brief")["exists"])
            self.assertFalse((self.root / ".relay").exists())

    def test_brief_degraded_counts_are_unknown_not_zero(self):
        current = self.root / ".relay/CURRENT.md"
        # A valid object reference with a missing object must not look gap-free.
        with mock.patch.object(cli, "_resolve_for_read", return_value=(None, cli.RelayError("RELAY_OBJECT_MISSING", "missing object"))):
            result = self.call("status", "--brief")
        self.assertIsNone(result["progress_summary"]["gap_count"])
        self.assertFalse(result["write_ready"])


class WarningTests(Case):
    def test_below_trigger_with_archivable_receipts_is_quiet(self):
        for revision in range(33):
            self.write(revision=revision, op="r" + str(revision), extra=("--no-auto-compact",))
        result = self.call("status")
        self.assertFalse(result["near_limit"])
        self.assertGreater(result["receipts"]["planned_segments"], 0)
        self.assertFalse(result["compact_recommended"])
        self.assertFalse(any("capacity governance recommended" in w for w in result["warnings"]))

    def test_history_and_tombstone_hints_are_advisory(self):
        self.write(op="r")
        extensions = {f"round_{i}": {"archived": True, "archive_ref": "payloads"} for i in range(25)}
        extensions["payloads"] = {"pointer": "external-report"}
        self.write("save", 1, op="s", patch={"extensions": extensions})
        history = self.root / ".relay/history"
        for index in range(101):
            (history / (f"r{index}-" + "a" * 64 + ".md")).write_text("fixture", encoding="utf-8")
        before = (self.root / ".relay/CURRENT.md").read_bytes()
        status = self.call("status")
        self.assertTrue(any("gc" in w and "preview" in w for w in status["warnings"]))
        self.assertTrue(any('null' in w and 'extensions' in w for w in status["warnings"]))
        self.assertEqual((self.root / ".relay/CURRENT.md").read_bytes(), before)
        self.assertIn("round_0", cli.read(self.root)[4]["extensions"])


class OperationTests(Case):
    def test_auto_resume_save_retry_and_current_revision(self):
        first = self.write()
        self.assertTrue(first["operation_id"].startswith("auto-"))
        replay = self.write()
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["operation_id"], replay["operation_id"])
        self.write("save", 1, patch={"project": {"next_step": "first"}})
        replay = self.write()
        self.assertEqual(replay["revision"], 1)
        self.assertEqual(replay["current_revision"], 2)
        self.assertIsNone(replay["writer"])
        self.assertIsNone(replay["lease_until"])

    def test_auto_update_normalizes_json_and_distinguishes_input(self):
        self.write(op="r")
        a = self.write("update", 1, patch={"project": {"next_step": "one", "status": "active"}})
        replay = self.write("update", 1, patch={"project": {"status": "active", "next_step": "one"}})
        self.assertEqual(a["operation_id"], replay["operation_id"])
        b = self.write("update", 2, patch={"project": {"next_step": "two"}})
        self.assertNotEqual(a["operation_id"], b["operation_id"])

    def test_explicit_id_retains_reuse_guard(self):
        first = self.write(op="explicit")
        self.assertEqual(first["operation_id"], "explicit")
        result = self.write(op="explicit", extra=("--lease-minutes", "31"), expected=2)
        self.assertIn("reused with different input", result["error"])

    def test_acquire_is_one_revision_and_one_history_snapshot(self):
        result = self.write("save", extra=("--acquire",), patch={"project": {"next_step": "single"}})
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["writer"], "null")
        status = self.call("status")
        self.assertIsNone(status["writer"])
        self.assertEqual(len(list((self.root / ".relay/history").iterdir())), 1)
        self.assertTrue(self.write("save", extra=("--acquire",), patch={"project": {"next_step": "single"}})["replayed"])

    def test_acquire_permits_own_live_and_expired_lease(self):
        self.write(op="r")
        self.write("save", 1, extra=("--acquire",))
        self.write(revision=2, writer="other", op="old")
        with mock.patch.object(cli, "now", return_value=datetime(2099, 1, 1, tzinfo=timezone.utc)):
            self.assertEqual(self.write("save", 3, extra=("--acquire",))["revision"], 4)

    def test_acquire_rejects_foreign_live_lease_and_keeps_state(self):
        self.write(writer="other", op="r")
        before = (self.root / ".relay/CURRENT.md").read_bytes()
        error = self.write("save", 1, extra=("--acquire",), expected=2)
        self.assertIn("writer=other", error["error"])
        self.assertEqual((self.root / ".relay/CURRENT.md").read_bytes(), before)

    def test_acquire_drift_check_matches_resume(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        changed = cli.git_state.capture(self.root)
        with mock.patch.object(cli.git_state, "capture", return_value=changed):
            error = self.write("save", extra=("--acquire",), expected=2)
            self.assertIn("Git drift detected", error["error"])
            self.assertEqual(self.write("save", extra=("--acquire", "--allow-drift"))["revision"], 1)

    def test_default_save_still_requires_lease(self):
        error = self.write("save", expected=2)
        self.assertIn("lease", error["error"])

    def test_acquire_invalid_patch_is_atomic(self):
        before = (self.root / ".relay/CURRENT.md").read_bytes()
        self.write("save", extra=("--acquire",), patch={"wrong": True}, expected=2)
        self.assertEqual((self.root / ".relay/CURRENT.md").read_bytes(), before)
        self.assertEqual(list((self.root / ".relay/history").iterdir()), [])

    def test_capacity_auto_resume_predicts_actual_bytes(self):
        preview = self.call("capacity", "--writer", "a", "--expected-revision", "0")
        result = self.write()
        self.assertEqual(preview["next_commit"]["model"]["operation_id"], result["operation_id"])
        self.assertEqual(preview["next_commit"]["candidate_bytes"], result["capacity"]["used_bytes"])
        replay = self.call("capacity", "--writer", "a", "--expected-revision", "0")
        self.assertTrue(replay["next_commit"]["replayed"])
        self.assertEqual(replay["next_commit"]["candidate_bytes"], result["capacity"]["used_bytes"])
        self.assertEqual(replay["next_commit"]["model"]["revision"], 1)

    def test_capacity_acquire_patch_matches_one_commit(self):
        patch = {"project": {"next_step": "one-commit"}}
        frozen = datetime(2026, 10, 16, tzinfo=timezone.utc)
        with mock.patch.object(cli, "now", return_value=frozen):
            preview = self.call("capacity", "--writer", "a", "--expected-revision", "0", "--acquire", patch=patch)
            result = self.write("save", extra=("--acquire",), patch=patch)
        model = preview["patch_preview"]
        self.assertEqual(model["commands"], ["save"])
        self.assertEqual(model["model"]["operation_ids"], [result["operation_id"]])
        self.assertEqual(model["final_candidate_bytes"], result["capacity"]["used_bytes"])
        with mock.patch.object(cli, "now", return_value=frozen):
            cycle = self.call("capacity", "--writer", "a", "--expected-revision", "1", patch=patch)["patch_preview"]
            resumed = self.write(revision=1)
            saved = self.write("save", 2, patch=patch)
        self.assertEqual(cycle["model"]["operation_ids"], [resumed["operation_id"], saved["operation_id"]])
        self.assertEqual(cycle["final_candidate_bytes"], saved["capacity"]["used_bytes"])

    def test_acquire_supports_all_existing_schemas(self):
        for version in ("v2", "v3", "v4", "v5", "v6"):
            with tempfile.TemporaryDirectory() as raw:
                saved, self.root = self.root, Path(raw).resolve()
                self.call("init", "--schema", version if version in ("v2", "v6") else "v2")
                revision = 0
                steps = {"v2": [], "v3": ["--to-v3"], "v4": ["--to-v4"],
                         "v5": ["--to-v4", "--to-v5"], "v6": []}[version]
                for flag in steps:
                    source = self.call("migrate", flag)["source_sha256"]
                    revision = self.write("migrate", revision, op="m" + flag,
                                          extra=(flag, "--apply", "--source-sha256", source))["revision"]
                result = self.write("save", revision, extra=("--acquire",), patch={"project": {"status": "active"}})
                self.assertEqual(result["schema"], "project-continuity/" + version)
                self.assertEqual(result["revision"], revision + 1)
                self.assertIsNone(self.call("status")["writer"])
                self.root = saved

    def test_auto_id_fingerprint_changes_for_same_revision_different_patch(self):
        args = cli.parser().parse_args(["save", "--acquire", "--writer", "a", "--expected-revision", "0"])
        first = cli._operation_identity(args, {"project": {"next_step": "a"}})[0]
        second = cli._operation_identity(args, {"project": {"next_step": "b"}})[0]
        self.assertNotEqual(first, second)
        args.acquire = False
        self.assertNotEqual(first, cli._operation_identity(args, {"project": {"next_step": "a"}})[0])

    def test_competing_acquire_writers_cannot_both_commit(self):
        import concurrent.futures
        script = str(Path(cli.__file__).with_name("write_current.py"))
        def run(writer):
            return subprocess.run([sys.executable, script, "save", "--acquire", "--root", str(self.root),
                                   "--writer", writer, "--expected-revision", "0"], capture_output=True, text=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, ["left", "right"]))
        self.assertEqual(sorted(r.returncode for r in results), [0, 2])
        self.assertEqual(self.call("status")["revision"], 1)
        self.assertIsNone(self.call("status")["writer"])

    def test_sensitive_commands_still_require_explicit_operation_id(self):
        for command, extra in (("recover", ("--reason", "audit")), ("prune", ()), ("compact", ("--apply",))):
            result = self.write(command, extra=extra, expected=2)
            self.assertIn("operation", result["error"])


class ExtensionsTests(Case):
    def add_extensions(self, values):
        self.write(op="r")
        self.write("save", 1, op="s", patch={"extensions": values})

    def test_capacity_lists_sizes_patch_and_preserved_targets_without_values(self):
        self.add_extensions({"old": {"archived": True, "archive_ref": "payloads"}, "payloads": {"report": "PRIVATE-CONTENT"}, "large": "x" * 1000})
        before = (self.root / ".relay/CURRENT.md").read_bytes()
        result = self.call("capacity")["extensions"]
        self.assertEqual(result["tombstones"]["keys"], ["old"])
        self.assertEqual(result["tombstones"]["count"], 1)
        self.assertGreater(result["tombstones"]["bytes"], 0)
        self.assertEqual(result["archive_patch"], {"extensions": {"old": None}})
        self.assertEqual(result["preserve_keys"], ["payloads"])
        self.assertEqual(result["largest"][0]["key"], "large")
        self.assertNotIn("PRIVATE-CONTENT", json.dumps(result))
        self.assertEqual((self.root / ".relay/CURRENT.md").read_bytes(), before)
        self.assertEqual(len(self.call("capacity", "--extension-limit", "1")["extensions"]["largest"]), 1)

    def test_exact_shape_only_and_dangling_is_reported(self):
        self.add_extensions({"extra": {"archived": True, "archive_ref": "payload", "note": "x"}, "string": {"archived": "true", "archive_ref": "payload"}, "empty": {"archived": True, "archive_ref": ""}, "dangling": {"archived": True, "archive_ref": "missing"}, "ok": {"archived": True, "archive_ref": "payload"}, "payload": "pointer"})
        result = self.call("capacity")["extensions"]["tombstones"]
        self.assertEqual(result["keys"], ["dangling", "ok"])
        self.assertEqual(result["dangling_keys"], ["dangling"])
        self.assertFalse(any("tombstones" in warning for warning in self.call("status")["warnings"]))

    def test_managed_and_referenced_keys_are_not_archive_candidates(self):
        shape = lambda target: {"archived": True, "archive_ref": target}
        extensions = {key: shape("target") for key in (*cli.p.MANAGED_EXTENSIONS, "migration", "migration_v5")}
        extensions.update({"outer": shape("target"), "target": shape("missing")})
        result = cli._extension_governance({"extensions": extensions}, cli.p.SCHEMA_V6)
        self.assertEqual(result["tombstones"]["keys"], ["outer"])
        self.assertNotIn("target", result["archive_patch"]["extensions"])

    def test_legacy_schema_advises_migration_instead_of_null_patch(self):
        for schema in (cli.p.SCHEMA_V2, cli.p.SCHEMA_V3, cli.p.SCHEMA_V4, cli.p.SCHEMA_V5):
            result = cli._extension_governance({"extensions": {"old": {"archived": True, "archive_ref": "external"}}}, schema)
            self.assertNotIn("archive_patch", result)
            self.assertIn("migrate", result["recommendation"])

    def test_extension_limit_is_bounded(self):
        result = self.call("capacity", "--extension-limit", "0", expected=2)
        self.assertIn("extension-limit", result["error"])
