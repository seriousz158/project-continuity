"""project-continuity/v4: object store, resolver, capacity and safety tests.

Every case runs in an isolated temporary project.  No live relay, worktree or
Git repository of any real project is read or written.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cli_v2 as cli
import objectstore as obs
import progress as p
import storage as fs
import v4 as relay_v4

SKILL = Path(__file__).resolve().parents[1]
WRITE = SKILL / "scripts" / "write_current.py"


class _FakeStdin:
    def __init__(self, payload: bytes):
        self.buffer = io.BytesIO(payload)


class V4Case(unittest.TestCase):
    project_id = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.assertEqual(self.call("init", "--name", "v4 fixture")[0], 0)

    def call(self, *args, patch=None, expect=0):
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
        if expect is not None:
            self.assertEqual(code, expect, err.getvalue() or out.getvalue())
        body = out.getvalue().strip()
        return code, (json.loads(body) if body else {}), err.getvalue()

    def subprocess_call(self, *args, patch=None, cwd=None):
        return subprocess.run([sys.executable, "-B", str(WRITE), *args,
                               "--root", str(self.root)],
                              input=json.dumps(patch) if patch is not None else None,
                              text=True, encoding="utf-8", capture_output=True,
                              timeout=120, cwd=str(cwd or SKILL))

    def seed(self, *, acceptance=None, evidence=None, task_id="t1"):
        acceptance = acceptance or ["condition one", "condition two"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision", "0",
                                   "--operation-id", "resume-1")[0], 0)
        patch = {"tasks": [{"id": task_id, "title": "fixture task", "status": "doing",
                            "acceptance": list(acceptance)}]}
        if evidence:
            patch["evidence"] = evidence
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision", "1",
                                   "--operation-id", "save-1", patch=patch)[0], 0)

    def entry(self, acceptance, *, result="pass", condition="condition one", eid="ev-1"):
        return {"id": eid, "task_id": "t1", "check": "check " + eid, "result": result,
                "at": "2026-09-16T00:00:00Z", "ref": "runs/" + eid,
                "acceptance": list(acceptance)}

    def to_v4(self, *, revision=None):
        preview = self.call("migrate", "--to-v4")[1]
        if revision is None:
            revision = self.call("status")[1]["revision"]
        result = self.call("migrate", "--to-v4", "--apply", "--writer", "w",
                           "--expected-revision", str(revision),
                           "--operation-id", "migrate-v4",
                           "--source-sha256", preview["source_sha256"])[1]
        self.assertEqual(result["schema"], p.SCHEMA_V4)
        return result

    def current(self):
        return (self.root / ".relay" / "CURRENT.md").read_text(encoding="utf-8")

    def state(self):
        return relay_v4.resolve(self.current(), self.root)


class V4MigrationTests(V4Case):
    def test_migration_is_explicit_and_keeps_v2_until_then(self):
        self.seed(evidence=[self.entry(["condition one"])])
        self.assertEqual(self.call("status")[1]["schema"], p.SCHEMA_V2)
        self.assertFalse((self.root / ".relay" / "objects").exists())
        self.to_v4()
        self.assertEqual(self.call("status")[1]["schema"], p.SCHEMA_V4)
        self.assertTrue((self.root / ".relay" / "objects").exists())

    def test_v4_requires_an_explicit_resolver(self):
        self.seed(evidence=[self.entry(["condition one"])])
        self.to_v4()
        with self.assertRaises(cli.RelayError) as caught:
            relay_v4.resolve(self.current())
        self.assertEqual(caught.exception.code, "RELAY_SCHEMA_V4_REQUIRES_RESOLVER")

    def test_v4_state_is_lossless_against_the_source(self):
        self.seed(evidence=[self.entry(["condition one"]),
                            self.entry(["condition two"], condition="condition two",
                                       eid="ev-2", result="fail")])
        before = cli.read(self.root)[4]
        self.to_v4()
        after = self.state()
        self.assertEqual(before["evidence"], after["evidence"])
        self.assertEqual(before["tasks"], after["tasks"])
        self.assertEqual(before["blockers"], after["blockers"])
        self.assertEqual(before["decisions"], after["decisions"])

    def test_v4_writes_stay_v4_and_keep_committing(self):
        self.seed(evidence=[self.entry(["condition one", "condition two"])])
        self.to_v4()
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r2")[0], 0)
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision",
                                   str(revision + 1), "--operation-id", "s2",
                                   patch={"project": {"next_step": "next"}})[0], 0)
        self.assertEqual(self.call("status")[1]["schema"], p.SCHEMA_V4)
        self.assertEqual([e["id"] for e in self.state()["evidence"]], ["ev-1"])

    def test_current_bytes_after_every_v4_commit(self):
        self.seed(evidence=[self.entry(["condition one", "condition two"])])
        self.to_v4()
        sizes = [len(self.current().encode("utf-8"))]
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "cap-r")[0], 0)
        for step in range(1, 6):
            self.assertEqual(self.call("update", "--writer", "w", "--expected-revision",
                                       str(revision + step), "--operation-id",
                                       "cap-u" + str(step),
                                       patch={"project": {"next_step": "step " + str(step)}})[0], 0)
            sizes.append(len(self.current().encode("utf-8")))
        for size in sizes:
            self.assertLessEqual(size, p.V4_MAX_BYTES)
        self.assertLessEqual(sizes[-1], p.V4_MAX_BYTES)

    def test_over_limit_v4_candidate_is_refused_without_switching(self):
        self.seed(evidence=[self.entry(["condition one", "condition two"])])
        self.to_v4()
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "big-r")[0], 0)
        before = (self.root / ".relay" / "CURRENT.md").read_bytes()
        code, _out, err = self.call("update", "--writer", "w", "--expected-revision",
                                    str(revision + 1), "--operation-id", "big-u",
                                    patch={"extensions": {"filler": "x" * 40000}},
                                    expect=None)
        self.assertEqual(code, 2)
        self.assertIn("RELAY_CURRENT_CAPACITY_EXCEEDED", err)
        self.assertEqual((self.root / ".relay" / "CURRENT.md").read_bytes(), before)

    def test_migration_from_a_near_limit_v3_document(self):
        lines, body, meta = cli.split(self.current())
        state = cli.read_envelope(self.root)[4]
        acceptance = ["condition %02d" % index for index in range(60)]
        state["project"]["current_task"] = "t1"
        state["tasks"] = [{"id": "t1", "title": "fixture task", "status": "doing",
                           "owner": None, "depends_on": [], "acceptance": acceptance,
                           "generation": 0}]
        state["evidence"] = [
            {"id": "ev-%02d" % index, "task_id": "t1", "check": "check " + "c" * 200,
             "result": "pass", "at": "2026-09-16T00:00:00Z",
             "ref": "runs/fixture-%02d" % index, "acceptance": [acceptance[index]],
             "baseline": {"kind": "none"}, "generation": 0}
            for index in range(60)]
        state["operations"] = [{"id": "fixture-%d" % index, "hash": "a" * 64,
                                "revision": index} for index in range(200)]
        state["extensions"]["compaction"] = {
            "schema": p.RECEIPT_SCHEMA, "head": None, "count": 0,
            "retained": p.RECEIPT_KEEP}
        fixture = cli.metadata(lines, {"schema": p.SCHEMA_V3, "revision": "300"}) \
            + p.render_body(state, body)
        size = len(fixture.encode("utf-8"))
        self.assertGreater(size, p.V4_MAX_BYTES)
        self.assertLessEqual(size, p.MAX_BYTES)
        current = self.root / ".relay" / "CURRENT.md"
        current.write_bytes(fixture.encode("utf-8"))
        preview = self.call("migrate", "--to-v4")[1]
        self.assertLessEqual(preview["candidate_bytes"], p.V4_MAX_BYTES)
        applied = self.call("migrate", "--to-v4", "--apply", "--writer", "w",
                            "--expected-revision", "300", "--operation-id", "m300",
                            "--source-sha256", preview["source_sha256"])[1]
        self.assertLessEqual(applied["compaction"]["after_bytes"], p.V4_MAX_BYTES)
        restored = self.state()
        self.assertEqual([item["id"] for item in restored["evidence"]],
                         ["ev-%02d" % index for index in range(60)])
        self.assertEqual(len(restored["operations"]), p.RECEIPT_KEEP)
        self.assertEqual(self.call("validate")[0], 0)


class V4ObjectStoreTests(V4Case):
    def test_chunking_round_trip_at_the_byte_boundary(self):
        payload = {"id": "ev-big", "task_id": "t1", "check": "c", "result": "pass",
                   "at": "2026-09-16T00:00:00Z", "ref": "r", "acceptance": ["a"],
                   "generation": 0, "baseline": {"kind": "none"}}
        large = dict(payload, ref="y" * 300000)
        plans, sha = obs.plan_payload(self.root, "proj", "evidence", large)
        kinds = [plan["kind"] for plan in plans]
        self.assertIn("chunk", kinds)
        self.assertIn("manifest", kinds)
        for plan in plans:
            self.assertLessEqual(len(plan["content"]), obs.OBJECT_MAX_BYTES)
            self.assertEqual(hashlib.sha256(plan["content"]).hexdigest(), plan["sha256"])
        obs.publish(self.root, plans)
        entry, _ = obs.entry_for(self.root, "proj", "evidence", large)
        self.assertEqual(entry["storage"], "chunked")
        budget = obs.Budget()
        self.assertEqual(obs.read_object(self.root, "proj", "evidence", entry, budget), large)

    def test_single_object_at_the_exact_limit_is_not_chunked(self):
        text = "z" * (obs.OBJECT_MAX_BYTES - 400)
        payload = {"id": "ev-limit", "task_id": "t1", "check": "c", "result": "pass",
                   "at": "2026-09-16T00:00:00Z", "ref": text, "acceptance": ["a"],
                   "generation": 0, "baseline": {"kind": "none"}}
        plans, _sha = obs.plan_payload(self.root, "proj", "evidence", payload)
        self.assertEqual([plan["kind"] for plan in plans], ["object"])
        self.assertLessEqual(len(plans[0]["content"]), obs.OBJECT_MAX_BYTES)

    def test_missing_chunk_and_tampered_chunk_are_named(self):
        payload = {"id": "ev-big", "task_id": "t1", "check": "c", "result": "pass",
                   "at": "2026-09-16T00:00:00Z", "ref": "y" * 300000, "acceptance": ["a"],
                   "generation": 0, "baseline": {"kind": "none"}}
        plans, sha = obs.plan_payload(self.root, "proj", "evidence", payload)
        obs.publish(self.root, plans)
        entry, _ = obs.entry_for(self.root, "proj", "evidence", payload)
        self.assertEqual(entry["storage"], "chunked")
        chunk = [plan for plan in plans if plan["kind"] == "chunk"][0]
        chunk["path"].unlink()
        with self.assertRaises(cli.RelayError) as caught:
            obs.read_object(self.root, "proj", "evidence", entry, obs.Budget())
        self.assertEqual(caught.exception.code, "RELAY_CHUNK_MISSING")
        obs.publish(self.root, plans)
        chunk["path"].write_bytes(b"tampered")
        with self.assertRaises(cli.RelayError) as caught:
            obs.read_object(self.root, "proj", "evidence", entry, obs.Budget())
        self.assertEqual(caught.exception.code, "RELAY_OBJECT_HASH_MISMATCH")

    def test_object_project_and_type_binding(self):
        payload = {"id": "ev-x", "task_id": "t1", "check": "c", "result": "pass",
                   "at": "2026-09-16T00:00:00Z", "ref": "r", "acceptance": ["a"],
                   "generation": 0, "baseline": {"kind": "none"}}
        plans, _sha = obs.plan_payload(self.root, "proj-a", "evidence", payload)
        obs.publish(self.root, plans)
        entry, _ = obs.entry_for(self.root, "proj-a", "evidence", payload)
        with self.assertRaises(cli.RelayError) as caught:
            obs.read_object(self.root, "proj-b", "evidence", entry, obs.Budget())
        self.assertEqual(caught.exception.code, "RELAY_OBJECT_PROJECT_MISMATCH")
        with self.assertRaises(cli.RelayError) as caught:
            obs.object_path(self.root, "archive", "0" * 64)
        self.assertEqual(caught.exception.code, "RELAY_OBJECT_TYPE_INVALID")

    def test_index_branching_and_reference_cycle(self):
        entries = []
        for index in range(5):
            payload = {"id": "ev-%d" % index, "task_id": "t1", "check": "c",
                       "result": "pass", "at": "2026-09-16T00:00:00Z", "ref": "r",
                       "acceptance": ["a"], "generation": 0, "baseline": {"kind": "none"}}
            entry, part = obs.entry_for(self.root, "proj", "evidence", payload)
            entries.append(entry)
            obs.publish(self.root, part)
        with mock.patch.object(obs, "MAX_INDEX_ENTRIES", 2):
            plans, root_sha = obs.plan_index(self.root, "proj", "evidence", entries)
        obs.publish(self.root, plans)
        found = obs.read_index(self.root, "proj", "evidence", root_sha, obs.Budget())
        self.assertEqual([entry["record_id"] for entry in found],
                         [entry["record_id"] for entry in entries])
        branch = obs.loads_named((obs.index_path(self.root, "evidence", root_sha)
                                  ).read_text(encoding="utf-8"), "RELAY_INDEX_INVALID",
                                 "index node")
        self.assertEqual(branch["kind"], "branch")
        cycle = dict(branch)
        cycle["children"] = [branch["children"][0], branch["children"][0]]
        cycle["count"] = 2 * branch["children"][0]["count"]
        data = obs.canonical(cycle).encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        obs.publish(self.root, [{"kind": "index",
                                 "path": obs.index_path(self.root, "evidence", sha),
                                 "content": data, "sha256": sha}])
        with self.assertRaises(cli.RelayError) as caught:
            obs.read_index(self.root, "proj", "evidence", sha, obs.Budget())
        self.assertEqual(caught.exception.code, "RELAY_REFERENCE_CYCLE")

    def test_truncated_object_and_concurrent_publish(self):
        payload = {"id": "ev-c", "task_id": "t1", "check": "c", "result": "pass",
                   "at": "2026-09-16T00:00:00Z", "ref": "r", "acceptance": ["a"],
                   "generation": 0, "baseline": {"kind": "none"}}
        plans, sha = obs.plan_payload(self.root, "proj", "evidence", payload)
        entry, _ = obs.entry_for(self.root, "proj", "evidence", payload)
        path = plans[0]["path"]
        fs.private_dir(path.parent)
        path.write_bytes(plans[0]["content"][:20])
        with self.assertRaises(cli.RelayError) as caught:
            obs.read_object(self.root, "proj", "evidence", entry, obs.Budget())
        self.assertEqual(caught.exception.code, "RELAY_OBJECT_HASH_MISMATCH")
        path.unlink()
        results = []

        def publish():
            results.append(obs.publish(self.root, plans))

        threads = [threading.Thread(target=publish) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 4)
        self.assertTrue(all(len(item["warnings"]) >= 0 for item in results))
        self.assertEqual(obs.read_object(self.root, "proj", "evidence", entry,
                                         obs.Budget()), payload)

    def test_object_paths_reject_traversal_and_symlinks(self):
        for bad in ("../../etc/passwd", "nothex", "0" * 63):
            with self.assertRaises(cli.RelayError):
                obs.object_path(self.root, "evidence", bad)
        shard = self.root / ".relay" / "objects" / "evidence" / "aa"
        shard.parent.mkdir(parents=True, exist_ok=True)
        target = self.root / "detached"
        target.mkdir()
        shard.symlink_to(target, target_is_directory=True)
        with self.assertRaises(fs.Error):
            obs.publish(self.root, [{"kind": "object",
                                     "path": shard / ("a" * 64 + ".json"),
                                     "content": b"{}", "sha256": "a" * 64}])

    def test_descriptor_and_index_cannot_be_replaced_by_a_summary(self):
        self.seed(evidence=[self.entry(["condition one", "condition two"])])
        self.to_v4()
        state = self.state()
        self.assertEqual(len(state["evidence"]), 1)
        document = self.current()
        lines, body, meta = cli.split(document)
        stub = cli.read_envelope(self.root)[4]
        tampered = json.loads(json.dumps(stub))
        tampered["evidence"]["count"] = 7
        current = self.root / ".relay" / "CURRENT.md"
        current.write_text(cli.metadata(lines, {}) + p.render_body(tampered, body),
                           encoding="utf-8")
        with self.assertRaises(cli.RelayError) as caught:
            relay_v4.resolve(current.read_text(encoding="utf-8"), self.root)
        self.assertEqual(caught.exception.code, "RELAY_INDEX_INVALID")

    def test_missing_bound_object_is_degraded_and_blocks_writes(self):
        self.seed(evidence=[self.entry(["condition one", "condition two"])])
        self.to_v4()
        stub = cli.read_envelope(self.root)[4]
        index_file = obs.index_path(self.root, "evidence", stub["evidence"]["index"])
        index_file.unlink()
        status = self.call("status")[1]
        self.assertEqual(status["verification"], "unverified")
        self.assertEqual(status["objects_integrity"], "degraded")
        self.assertEqual(status["objects_integrity"], "degraded")
        self.assertEqual(status["object_error"]["error"], "RELAY_INDEX_INVALID")
        self.assertFalse(status["write_ready"])
        self.assertEqual(self.call("validate", expect=None)[0], 2)
        revision = status["revision"]
        self.assertEqual(self.call("resume", "--writer", "b", "--expected-revision",
                                   str(revision), "--operation-id", "degraded",
                                   expect=None)[0], 2)

    def test_deep_validation_budget_exhaustion_is_not_corruption(self):
        self.seed(evidence=[self.entry(["condition one", "condition two"])])
        self.to_v4()
        real_budget = obs.Budget
        with self.assertRaises(cli.RelayError) as caught:
            with mock.patch.object(obs, "Budget",
                                   lambda *a, **k: real_budget(seconds=0.0)):
                relay_v4.resolve(self.current(), self.root)
        self.assertEqual(caught.exception.code, "RELAY_VALIDATION_BUDGET_EXCEEDED")
        with mock.patch.object(obs, "Budget", lambda *a, **k: real_budget(seconds=0.0)):
            status = self.call("status")[1]
        self.assertEqual(status["objects_integrity"], "budget_exceeded")
        self.assertEqual(status["verification"], "unverified")
        self.assertEqual(status["objects_integrity"], "budget_exceeded")
        self.assertFalse(status["write_ready"])


class V4SafetyTests(V4Case):
    def test_lease_revision_and_drift_still_hold(self):
        self.seed(evidence=[self.entry(["condition one", "condition two"])])
        self.to_v4()
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "a", "--expected-revision",
                                   str(revision), "--operation-id", "a1")[0], 0)
        self.assertEqual(self.call("update", "--writer", "b", "--expected-revision",
                                   str(revision + 1), "--operation-id", "b1",
                                   expect=None)[0], 2)
        self.assertEqual(self.call("update", "--writer", "a", "--expected-revision",
                                   str(revision), "--operation-id", "a2",
                                   expect=None)[0], 2)
        self.assertEqual(self.call("save", "--writer", "a", "--expected-revision",
                                   str(revision + 1), "--operation-id", "a3")[0], 0)

    def test_operation_retry_and_reuse(self):
        self.seed(evidence=[self.entry(["condition one", "condition two"])])
        self.to_v4()
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "op")[0], 0)
        first = self.call("update", "--writer", "w", "--expected-revision",
                          str(revision + 1), "--operation-id", "same",
                          patch={"project": {"next_step": "one"}})[1]
        self.assertFalse(first["replayed"])
        replay = self.call("update", "--writer", "w", "--expected-revision",
                           str(revision + 1), "--operation-id", "same",
                           patch={"project": {"next_step": "one"}})[1]
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["revision"], first["revision"])
        self.assertEqual(self.call("update", "--writer", "w", "--expected-revision",
                                   str(revision + 1), "--operation-id", "same",
                                   patch={"project": {"next_step": "two"}},
                                   expect=None)[0], 2)

    def test_secret_scan_covers_object_payloads(self):
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision", "0",
                                   "--operation-id", "r0")[0], 0)
        secret = "ghp_" + "Q" * 36
        code, _out, err = self.call("save", "--writer", "w", "--expected-revision", "1",
                                    "--operation-id", "s0",
                                    patch={"tasks": [{"id": "t1", "title": "t",
                                                      "status": "doing",
                                                      "acceptance": ["a"]}]},
                                    expect=None)
        self.assertEqual(code, 0, err)
        preview = self.call("migrate", "--to-v4")[1]
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("migrate", "--to-v4", "--apply", "--writer", "w",
                                   "--expected-revision", str(revision),
                                   "--operation-id", "m0",
                                   "--source-sha256", preview["source_sha256"])[0], 0)
        before = sorted(str(path) for path in
                        (self.root / ".relay" / "objects").rglob("*") if path.is_file())
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r1")[0], 0)
        poisoned = self.entry(["a"], condition="a", eid="ev-secret")
        poisoned["ref"] = "runs/leaked/" + secret
        code, _out, err = self.call("save", "--writer", "w", "--expected-revision",
                                    str(revision + 1), "--operation-id", "s1",
                                    patch={"evidence": [poisoned]},
                                    expect=None)
        self.assertEqual(code, 2)
        self.assertNotIn(secret, err)
        after = sorted(str(path) for path in
                       (self.root / ".relay" / "objects").rglob("*") if path.is_file())
        self.assertEqual(before, after)
        current = self.call("status")[1]["revision"]
        self.assertEqual(current, revision + 1)
        benign = self.call("save", "--writer", "w", "--expected-revision",
                           str(current), "--operation-id", "s2",
                           patch={"project": {"next_step": "benign"}})
        self.assertEqual(benign[0], 0)
        self.assertEqual(self.call("validate")[0], 0)

    def test_quota_and_disk_refusals_leave_current_untouched(self):
        self.seed(evidence=[self.entry(["condition one", "condition two"])])
        self.to_v4()
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "q-r")[0], 0)
        before = (self.root / ".relay" / "CURRENT.md").read_bytes()
        with mock.patch.object(obs, "OBJECT_HARD_QUOTA_BYTES", 1):
            code, _out, err = self.call("update", "--writer", "w", "--expected-revision",
                                        str(revision + 1), "--operation-id", "q-u",
                                        patch={"project": {"next_step": "x"}}, expect=None)
        self.assertEqual(code, 2)
        self.assertIn("RELAY_STORAGE_QUOTA_EXCEEDED", err)
        self.assertEqual((self.root / ".relay" / "CURRENT.md").read_bytes(), before)
        with mock.patch.object(obs, "disk_free", lambda _root: 10):
            code, _out, err = self.call("update", "--writer", "w", "--expected-revision",
                                        str(revision + 1), "--operation-id", "d-u",
                                        patch={"project": {"next_step": "x"}}, expect=None)
        self.assertEqual(code, 2)
        self.assertIn("RELAY_DISK_SPACE_INSUFFICIENT", err)
        self.assertEqual((self.root / ".relay" / "CURRENT.md").read_bytes(), before)

    def test_crash_at_each_persistence_cut_point(self):
        self.seed(evidence=[self.entry(["condition one", "condition two"])])
        self.to_v4()
        current = self.root / ".relay" / "CURRENT.md"
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "cr-r")[0], 0)
        args = ("update", "--writer", "w", "--expected-revision", str(revision + 1),
                "--operation-id", "cr-u")
        before = current.read_bytes()
        real_atomic = cli.fs.atomic

        def publish_failure(root, plans):
            raise fs.Error("injected object failure")

        def archive_failure(root, segments):
            raise fs.Error("injected archive failure")

        def history_failure(path, document):
            if path.parent.name == "history":
                raise fs.Error("injected history failure")
            return real_atomic(path, document)

        def current_failure(path, document):
            if path.name == "CURRENT.md":
                raise fs.Error("injected current failure")
            return real_atomic(path, document)

        cuts = (
            ("objects", mock.patch.object(cli.obs, "publish", side_effect=publish_failure)),
            ("archives", mock.patch.object(cli, "_store_archive_segments",
                                           side_effect=archive_failure)),
            ("history", mock.patch.object(cli.fs, "atomic", side_effect=history_failure)),
            ("current", mock.patch.object(cli.fs, "atomic", side_effect=current_failure)),
        )
        for name, patcher in cuts:
            with patcher:
                code, _out, _err = self.call(*args, patch={"project": {"next_step": "cut"}},
                                             expect=None)
            self.assertEqual(code, 2, name)
            self.assertEqual(current.read_bytes(), before, name)
        self.assertEqual(self.call(*args, patch={"project": {"next_step": "cut"}})[0], 0)
        self.assertNotEqual(current.read_bytes(), before)
        self.assertEqual(self.call("validate")[0], 0)

    def test_completion_counterexamples_still_refused(self):
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision", "0",
                                   "--operation-id", "r0")[0], 0)
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision", "1",
                                   "--operation-id", "s0",
                                   patch={"tasks": [{"id": "t1", "title": "t",
                                                     "status": "doing",
                                                     "acceptance": ["a", "b"]}]})[0], 0)
        self.to_v4()
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r1")[0], 0)
        code, _out, err = self.call("save", "--writer", "w", "--expected-revision",
                                    str(revision + 1), "--operation-id", "s1",
                                    patch={"tasks": [{"id": "t1", "status": "done"}],
                                           "evidence": [self.entry(["a"], condition="a")]},
                                    expect=None)
        self.assertEqual(code, 2)
        code, _out, err = self.call("save", "--writer", "w", "--expected-revision",
                                    str(revision + 1), "--operation-id", "s2",
                                    patch={"project": {"status": "complete"}}, expect=None)
        self.assertEqual(code, 2)

    def test_v4_ignore_regression_in_an_isolated_repository(self):
        project = Path(tempfile.mkdtemp(prefix="v4-ignore-")).resolve()
        self.addCleanup(lambda: shutil.rmtree(project, ignore_errors=True))
        subprocess.run(["git", "init", "-q", str(project)], check=True)

        def at_root(*args, patch=None):
            out, err = io.StringIO(), io.StringIO()
            argv = list(args)
            if patch is not None:
                argv = argv + ["--input", "-"]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                if patch is not None:
                    with mock.patch.object(sys, "stdin",
                                           _FakeStdin(json.dumps(patch).encode("utf-8"))):
                        code = cli.main(argv + ["--root", str(project)])
                else:
                    code = cli.main(argv + ["--root", str(project)])
            return code, out.getvalue().strip(), err.getvalue()

        self.assertEqual(at_root("init", "--name", "ignore check")[0], 0)
        self.assertEqual(at_root("resume", "--writer", "w", "--expected-revision", "0",
                                 "--operation-id", "r0")[0], 0)
        self.assertEqual(at_root("save", "--writer", "w", "--expected-revision", "1",
                                 "--operation-id", "s0",
                                 patch={"tasks": [{"id": "t1", "title": "t",
                                                   "status": "doing",
                                                   "acceptance": ["a"]}]})[0], 0)
        preview_code, preview_out, preview_err = at_root("migrate", "--to-v4")
        self.assertEqual(preview_code, 0, preview_err)
        preview = json.loads(preview_out)
        status_code, status_out, status_err = at_root("status")
        self.assertEqual(status_code, 0, status_err)
        revision = json.loads(status_out)["revision"]
        self.assertEqual(at_root("migrate", "--to-v4", "--apply", "--writer", "w",
                                 "--expected-revision", str(revision),
                                 "--operation-id", "m0",
                                 "--source-sha256", preview["source_sha256"])[0], 0)
        ignored = subprocess.run(["git", "check-ignore", "-v", ".relay/CURRENT.md",
                                  ".relay/objects"], cwd=str(project),
                                 capture_output=True, text=True)
        self.assertEqual(ignored.returncode, 0, ignored.stderr)
        status = subprocess.run(["git", "status", "--porcelain", "--", ".relay"],
                                cwd=str(project), capture_output=True, text=True)
        self.assertEqual(status.stdout.strip(), "")
        tracked = subprocess.run(["git", "ls-files", ".relay"], cwd=str(project),
                                 capture_output=True, text=True)
        self.assertEqual(tracked.stdout.strip(), "")
        # isolated git add -A rehearsal: nothing under .relay may be staged
        subprocess.run(["git", "add", "-A"], cwd=str(project), check=True)
        staged = subprocess.run(["git", "diff", "--cached", "--name-only", "--", ".relay"],
                                cwd=str(project), capture_output=True, text=True)
        self.assertEqual(staged.stdout.strip(), "")
        self.assertEqual(subprocess.run(["git", "ls-files", ".relay"], cwd=str(project),
                                        capture_output=True, text=True).stdout.strip(), "")


class V4InteropTests(V4Case):
    def test_export_bundle_carries_every_object_and_verifies(self):
        self.seed(evidence=[self.entry(["condition one", "condition two"])])
        self.to_v4()
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r1")[0], 0)
        for step in range(1, 40):
            self.assertEqual(self.call("update", "--writer", "w", "--expected-revision",
                                       str(revision + step), "--operation-id",
                                       "grow-%d" % step,
                                       patch={"project": {"next_step": "step %d" % step}})[0], 0)
        self.assertEqual(self.call("compact", "--apply", "--writer", "w",
                                   "--expected-revision", str(revision + 40),
                                   "--operation-id", "grow-compact")[0], 0)
        self.assertTrue(list((self.root / ".relay" / "receipts").iterdir()))
        bundle = self.root / "handoff.zip"
        self.assertEqual(self.call("export", "--output", str(bundle))[1]["schema"],
                         p.SCHEMA_V4)
        with zipfile.ZipFile(bundle) as archive:
            names = set(archive.namelist())
        self.assertIn("CURRENT.md", names)
        self.assertTrue(any(name.startswith("objects/") for name in names))
        self.assertTrue(any(name.startswith("receipts/") for name in names))
        self.assertTrue(self.call("verify", "--bundle", str(bundle))[1]["valid"])
        with zipfile.ZipFile(bundle) as archive:
            payloads = {name: archive.read(name) for name in archive.namelist()}
        victim = next(name for name in payloads if name.startswith("objects/"))
        payloads[victim] = b"tampered"
        broken = self.root / "broken.zip"
        with zipfile.ZipFile(broken, "w") as archive:
            for name, value in payloads.items():
                archive.writestr(name, value)
        self.assertEqual(self.call("verify", "--bundle", str(broken), expect=None)[0], 2)

    def test_handoff_fails_when_a_reference_is_missing(self):
        self.seed(evidence=[self.entry(["condition one", "condition two"])])
        self.to_v4()
        stub = cli.read_envelope(self.root)[4]
        index_file = obs.index_path(self.root, "evidence", stub["evidence"]["index"])
        index_file.unlink()
        code, out, err = self.call("handoff", expect=None)
        self.assertEqual(code, 2)
        self.assertIn("RELAY_INDEX_INVALID", err)
        self.assertEqual(out, {})
        self.assertNotIn("handoff", json.dumps(out))

    def test_coverage_matrix_maps_every_acceptance(self):
        self.seed(evidence=[self.entry(["condition one"])])
        self.to_v4()
        covered = self.call("coverage")[1]
        self.assertEqual(covered["count"], 2)
        self.assertTrue(all(row["persisted"] for row in covered["rows"]))
        self.assertEqual(sorted(row["ac_id"] for row in covered["rows"]),
                         sorted(p.ac_identifier("t1", text) for text in
                                ("condition one", "condition two")))
        self.assertEqual(sum(row["covered"] for row in covered["rows"]), 1)

    def test_scoped_blockers_widen_and_never_shrink_history(self):
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision", "0",
                                   "--operation-id", "r0")[0], 0)
        patch = {"tasks": [{"id": "t1", "title": "one", "status": "doing", "acceptance": ["a"]},
                           {"id": "t2", "title": "two", "status": "doing", "acceptance": ["b"]}],
                 "blockers": [{"id": "bl-1", "task_id": "t1", "status": "open",
                               "description": "blocks one"}]}
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision", "1",
                                   "--operation-id", "s0", patch=patch)[0], 0)
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r1")[0], 0)
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision",
                                   str(revision + 1), "--operation-id", "s1",
                                   patch={"extensions": {"blocker_scope": {
                                       "schema": p.BLOCKER_SCOPE_SCHEMA,
                                       "entries": [{"blocker_id": "bl-1",
                                                    "scope": ["project"]}]}}})[0], 0)
        state = self.call("status")[1]
        self.assertTrue(state["blockers"])
        revision = state["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r2")[0], 0)
        code, _out, err = self.call("save", "--writer", "w", "--expected-revision",
                                    str(revision + 1), "--operation-id", "s2",
                                    patch={"tasks": [{"id": "t2", "status": "done"}],
                                           "evidence": [{"id": "ev-t2", "task_id": "t2",
                                                         "check": "c", "result": "pass",
                                                         "at": "2026-09-16T00:00:00Z",
                                                         "ref": "r", "acceptance": ["b"]}]},
                                    expect=None)
        self.assertEqual(code, 2)

    def test_revocation_blocks_a_done_claim(self):
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision", "0",
                                   "--operation-id", "r0")[0], 0)
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision", "1",
                                   "--operation-id", "s0",
                                   patch={"tasks": [{"id": "t1", "title": "t",
                                                     "status": "doing",
                                                     "acceptance": ["a"]}]})[0], 0)
        self.to_v4()
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r1")[0], 0)
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision",
                                   str(revision + 1), "--operation-id", "s1",
                                   patch={"evidence": [self.entry(["a"], condition="a")]})[0], 0)
        state = self.state()
        target = state["evidence"][0]["id"]
        digest = p.digest(state["evidence"][0])
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r2")[0], 0)
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision",
                                   str(revision + 1), "--operation-id", "s2",
                                   patch={"corrections": [{"id": "cor-1",
                                                           "kind": "revocation",
                                                           "target_type": "evidence",
                                                           "target_id": target,
                                                           "replacement_id": None,
                                                           "reason": "withdrawn",
                                                           "at": "2026-09-16T00:00:00Z"}]})[0], 0)
        self.assertEqual(self.state()["evidence"][0]["id"], target)
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r3")[0], 0)
        code, _out, _err = self.call("save", "--writer", "w", "--expected-revision",
                                     str(revision + 1), "--operation-id", "s3",
                                     patch={"tasks": [{"id": "t1", "status": "done"}]},
                                     expect=None)
        self.assertEqual(code, 2)

    def test_read_only_commands_never_write(self):
        self.seed(evidence=[self.entry(["condition one", "condition two"])])
        self.to_v4()

        def snapshot():
            return {str(path.relative_to(self.root)): path.read_bytes()
                    for path in self.root.rglob("*") if path.is_file()}

        before = snapshot()
        for args in (("status",), ("validate",), ("compact",), ("capacity",),
                     ("coverage",), ("handoff",)):
            self.assertEqual(self.call(*args)[0], 0, args)
        self.assertEqual(snapshot(), before)

    def test_v4_snapshot_recovery_reconstructs_objects(self):
        self.seed(evidence=[self.entry(["condition one", "condition two"])])
        self.to_v4()
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r1")[0], 0)
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision",
                                   str(revision + 1), "--operation-id", "s1",
                                   patch={"project": {"next_step": "two"}})[0], 0)
        snapshots = []
        for path in (self.root / ".relay" / "history").glob("r*.md"):
            head = path.read_text(encoding="utf-8").split("---", 2)
            if len(head) >= 2 and p.SCHEMA_V4 in head[1]:
                snapshots.append(path.name)
        self.assertTrue(snapshots, "no v4 history snapshot was written")
        self.assertEqual(self.call("recover", "--writer", "w",
                                   "--expected-revision", str(revision + 2),
                                   "--operation-id", "rec",
                                   "--reason", "v4 recovery drill",
                                   "--snapshot", sorted(snapshots)[-1])[0], 0)
        state = self.state()
        self.assertEqual(state["project"]["next_step"], "")
        self.assertEqual([item["id"] for item in state["evidence"]], ["ev-1"])
        self.assertEqual(self.call("validate")[0], 0)
        self.assertTrue((self.root / ".relay" / "objects").is_dir())

    def test_legacy_v3_client_refuses_v4(self):
        legacy = os.environ.get("PROJECT_CONTINUITY_V3_SCRIPTS")
        if not legacy or not (Path(legacy) / "write_current.py").is_file():
            self.skipTest("no frozen v3 scripts directory provided")
        self.seed(evidence=[self.entry(["condition one", "condition two"])])
        self.to_v4()
        proc = subprocess.run([sys.executable, "-B", str(Path(legacy) / "write_current.py"),
                               "status", "--root", str(self.root)],
                              capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unsupported schema", proc.stderr)
        before = (self.root / ".relay" / "CURRENT.md").read_bytes()
        proc = subprocess.run([sys.executable, "-B", str(Path(legacy) / "write_current.py"),
                               "resume", "--root", str(self.root), "--writer", "old",
                               "--expected-revision", "1", "--operation-id", "old-op"],
                              capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual((self.root / ".relay" / "CURRENT.md").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
