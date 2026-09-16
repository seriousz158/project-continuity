"""Shared effective-evidence judgement: revocations, supersessions, coverage,
handoff and manifest correction targets.

This file is the Red-first matrix for the relay-v4 incremental repair.  Every
counterexample runs in memory or in an isolated temporary project; no live
relay, object store or Git repository of a real project is read or written.
"""
from __future__ import annotations

import contextlib
import copy
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
import objectstore as obs
import progress as p
import v4 as relay_v4

SKILL = Path(__file__).resolve().parents[1]
WRITE = SKILL / "scripts" / "write_current.py"
BASELINE = {"kind": "none"}


def evidence(record_id, result, acceptance=("a", "b"), task_id="t1", check="check", ref="ref"):
    return {"id": record_id, "task_id": task_id, "check": check, "result": result,
            "at": "2026-09-16T00:00:00Z", "ref": ref, "acceptance": list(acceptance)}


def relation(record_id, kind, target_type, target_id, replacement=None, reason="counterexample"):
    return {"id": record_id, "kind": kind, "target_type": target_type,
            "target_id": target_id, "replacement_id": replacement, "reason": reason,
            "at": "2026-09-16T01:00:00Z"}


def base_state(acceptance=("a", "b"), records=(), relations=(), status=None):
    """Build a validated in-memory state (never a relay document)."""
    state = p.empty_state("probe")
    state = p.apply(state, {"tasks": [{"id": "t1", "title": "probe task", "status": "doing",
                                       "acceptance": list(acceptance)}]}, BASELINE)
    if records:
        state = p.apply(state, {"evidence": list(records)}, BASELINE)
    if relations:
        state = p.apply(state, {"corrections": list(relations)}, BASELINE)
    if status is not None:
        state = p.apply(state, {"tasks": [{"id": "t1", "status": status}]}, BASELINE)
    return state


def coverage_rows(state):
    matrix = relay_v4.coverage(state, "probe")
    return {row["text"]: row for row in matrix["rows"]}


def uncovered(state):
    view = relay_v4.handoff(state, {"revision": "1", "schema": p.SCHEMA_V4}, "probe", "0" * 64)
    items = {item["ac_id"]: item
             for task in view["tasks"] for item in task.get("acceptance_items", [])}
    gaps = []
    for gap in view["unverified"]["uncovered_acceptance"]:
        merged = dict(gap)
        merged.update(items.get(gap["ac_id"], {}))
        gaps.append(merged)
    return gaps


class RevocationConsistencyTests(unittest.TestCase):
    """R1/R2: one revoked pass must not satisfy, or appear to satisfy, a gate."""

    def revoked_state(self):
        return base_state(records=[evidence("e1", "pass")],
                          relations=[relation("c1", "revocation", "evidence", "e1")])

    def test_r1_done_is_rejected_and_coverage_agrees(self):
        state = self.revoked_state()
        with self.assertRaises(p.Invalid):
            p.apply(state, {"tasks": [{"id": "t1", "status": "done"}]}, BASELINE)
        rows = coverage_rows(state)
        self.assertEqual(sorted(rows), ["a", "b"])
        for condition, row in rows.items():
            self.assertFalse(row["covered"], "revoked pass still reports covered for " + condition)
        self.assertEqual(rows["a"]["withdrawn"][0]["id"], "e1")
        self.assertEqual(rows["a"]["withdrawn"][0]["current"], "revoked")
        self.assertIn("e1", rows["a"]["recorded_contributors"])

    def test_r2_handoff_reports_the_revocation_gap(self):
        gaps = uncovered(self.revoked_state())
        self.assertEqual(len(gaps), 2, "handoff hides the revocation gap")
        self.assertEqual({gap["task_id"] for gap in gaps}, {"t1"})
        self.assertEqual({gap["current_reason"] for gap in gaps}, {"revoked"})

    def test_r2_handoff_gap_lists_the_withdrawn_record(self):
        gaps = uncovered(self.revoked_state())
        self.assertEqual(sorted(item["id"] for item in gaps[0]["withdrawn"]), ["e1"])

    def test_another_effective_pass_still_covers(self):
        state = base_state(records=[evidence("e1", "pass"), evidence("e2", "pass")],
                           relations=[relation("c1", "revocation", "evidence", "e1")])
        for condition, row in coverage_rows(state).items():
            self.assertTrue(row["covered"], "a valid pass was discarded for " + condition)
        p.apply(state, {"tasks": [{"id": "t1", "status": "done"}]}, BASELINE)
        self.assertEqual(uncovered(state), [])


class SupersessionConsistencyTests(unittest.TestCase):
    """R3: a superseded pass never contributes; the replacement decides."""

    def test_r3_superseded_pass_stops_contributing(self):
        state = base_state(records=[evidence("e1", "pass"), evidence("e2", "fail", acceptance=("a",))],
                           relations=[relation("c1", "supersession", "evidence", "e1", "e2")])
        rows = coverage_rows(state)
        self.assertFalse(rows["a"]["covered"], "superseded pass still contributes")
        self.assertFalse(rows["b"]["covered"])
        self.assertEqual(rows["a"]["withdrawn"][0]["current"], "superseded")
        self.assertEqual(rows["a"]["withdrawn"][0]["superseded_by"], "e2")
        gaps = uncovered(state)
        self.assertEqual(len(gaps), 2, "handoff hides the supersession gap")

    def test_r3_supersession_to_a_valid_pass_keeps_coverage(self):
        state = base_state(records=[evidence("e1", "pass"), evidence("e2", "pass")],
                           relations=[relation("c1", "supersession", "evidence", "e1", "e2")])
        for condition, row in coverage_rows(state).items():
            self.assertTrue(row["covered"], "replacement pass did not cover " + condition)
            self.assertEqual(row["contributors"], ["e2"])
        self.assertEqual(uncovered(state), [])

    def test_supersession_cycle_is_rejected(self):
        state = base_state(records=[evidence("e1", "pass"), evidence("e2", "pass")])
        first = p.apply(state, {"corrections": [relation("c1", "supersession", "evidence", "e1", "e2")]},
                        BASELINE)
        with self.assertRaises(p.Invalid):
            p.apply(first, {"corrections": [relation("c2", "supersession", "evidence", "e2", "e1")]},
                    BASELINE)

    def test_supersession_replacement_must_be_resolved_evidence(self):
        state = base_state(records=[evidence("e1", "pass")])
        state = p.apply(state, {"decisions": [{"id": "d1", "task_ids": ["t1"],
                                               "conclusion": "c", "reason": "r"}]}, BASELINE)
        with self.assertRaises(p.Invalid):
            p.apply(state, {"corrections": [relation("c1", "supersession", "evidence", "e1", "d1")]},
                    BASELINE)

    def test_supersession_target_must_be_evidence(self):
        state = base_state(records=[evidence("e1", "pass")])
        state = p.apply(state, {"decisions": [{"id": "d1", "task_ids": ["t1"],
                                               "conclusion": "c", "reason": "r"}]}, BASELINE)
        with self.assertRaises(p.Invalid):
            p.apply(state, {"corrections": [relation("c1", "supersession", "decision", "d1", "e1")]},
                    BASELINE)


class TypedIdentityTests(unittest.TestCase):
    """Relationship identity is (record_type, record_id), never a bare id."""

    def test_revoking_a_decision_does_not_revoke_an_evidence(self):
        state = base_state(records=[evidence("x1", "pass")])
        state = p.apply(state, {"decisions": [{"id": "x1", "task_ids": ["t1"],
                                               "conclusion": "same string id",
                                               "reason": "collision probe"}]}, BASELINE)
        state = p.apply(state, {"corrections": [relation("c1", "revocation", "decision", "x1")]},
                        BASELINE)
        for condition, row in coverage_rows(state).items():
            self.assertTrue(row["covered"], "a decision revocation removed evidence for " + condition)

    def test_revoking_an_evidence_does_not_touch_a_same_id_decision(self):
        state = base_state(records=[evidence("x1", "pass")])
        state = p.apply(state, {"decisions": [{"id": "x1", "task_ids": ["t1"],
                                               "conclusion": "same string id",
                                               "reason": "collision probe"}]}, BASELINE)
        state = p.apply(state, {"corrections": [relation("c1", "revocation", "evidence", "x1")]},
                        BASELINE)
        self.assertEqual([d["id"] for d in state["decisions"]], ["x1"])
        self.assertFalse(coverage_rows(state)["a"]["covered"])

    def test_a_correction_may_annotate_a_correction(self):
        state = base_state(records=[evidence("e1", "pass")],
                           relations=[relation("c1", "revocation", "evidence", "e1")])
        state = p.apply(state, {"corrections": [relation("c2", "correction", "correction", "c1",
                                                         reason="note about c1")]}, BASELINE)
        self.assertEqual(len(state["corrections"]), 2)
        self.assertFalse(coverage_rows(state)["a"]["covered"])

    def test_revoking_a_correction_is_refused(self):
        state = base_state(records=[evidence("e1", "pass")],
                           relations=[relation("c1", "revocation", "evidence", "e1")])
        with self.assertRaises(p.Invalid):
            p.apply(state, {"corrections": [relation("c2", "revocation", "correction", "c1")]},
                    BASELINE)

    def test_a_correction_cannot_target_itself(self):
        state = base_state(records=[evidence("e1", "pass")])
        with self.assertRaises(p.Invalid):
            p.apply(state, {"corrections": [relation("c1", "correction", "correction", "c1")]},
                    BASELINE)

    def test_duplicate_relation_id_with_different_content_is_refused(self):
        state = base_state(records=[evidence("e1", "pass")],
                           relations=[relation("c1", "revocation", "evidence", "e1")])
        with self.assertRaises(p.Invalid):
            p.apply(state, {"corrections": [relation("c1", "revocation", "evidence", "e1",
                                                     reason="different reason")]}, BASELINE)


class GenerationAndBaselineTests(unittest.TestCase):
    """L3 and L4 are separate: covered and verified are different questions."""

    def reopened_state(self):
        state = base_state(records=[evidence("e1", "pass")], status="done")
        return p.apply(state, {"tasks": [{"id": "t1", "status": "doing", "reason": "reopen"}]},
                       BASELINE)

    def test_generation_mismatch_is_named(self):
        state = self.reopened_state()
        self.assertEqual(state["tasks"][0]["generation"], 1)
        view = p.evidence_view(state)
        self.assertFalse(view["records"][0]["generation_match"])
        self.assertFalse(view["records"][0]["contributes"])
        self.assertEqual(view["records"][0]["reason"], "generation_mismatch")
        for row in coverage_rows(state).values():
            self.assertFalse(row["covered"])

    def test_unchecked_baseline_is_named_and_not_assumed(self):
        state = base_state(records=[evidence("e1", "pass")])
        rows = coverage_rows(state)
        self.assertTrue(rows["a"]["covered"])
        self.assertFalse(rows["a"]["baseline_verified"])
        self.assertEqual(rows["a"]["baseline_reason"], "baseline_not_checked")
        self.assertFalse(rows["a"]["verified"])

    def test_mismatching_baseline_does_not_remove_coverage(self):
        state = base_state(records=[evidence("e1", "pass")])
        rows = relay_v4.coverage(state, "probe", baseline={"kind": "none", "fingerprint": "other"})
        row = {r["text"]: r for r in rows["rows"]}["a"]
        self.assertTrue(row["covered"], "a stale baseline must not delete coverage")
        self.assertFalse(row["baseline_verified"])
        self.assertEqual(row["baseline_reason"], "baseline_mismatch")

    def test_recorded_and_current_coverage_are_both_reported(self):
        state = base_state(records=[evidence("e1", "pass")],
                           relations=[relation("c1", "revocation", "evidence", "e1")])
        row = coverage_rows(state)["a"]
        self.assertTrue(row["recorded_covered"])
        self.assertFalse(row["covered"])

    def test_failing_record_is_named(self):
        state = base_state(records=[evidence("e1", "fail")])
        view = p.evidence_view(state)
        self.assertEqual(view["records"][0]["recorded_result"], "fail")
        self.assertEqual(view["records"][0]["reason"], "result_fail")


class ManifestTargetTests(unittest.TestCase):
    """R4: a syntactically valid hash is never accepted as a verified target."""

    def test_r4_ambiguous_manifest_target_is_refused(self):
        state = base_state(records=[evidence("e1", "pass")])
        with self.assertRaises(cli.RelayError) as caught:
            p.apply(state, {"corrections": [relation("c9", "revocation", "manifest", "f" * 64)]},
                    BASELINE)
        self.assertEqual(caught.exception.code, "RELAY_CORRECTION_TARGET_UNSUPPORTED")

    def test_unknown_manifest_target_type_is_refused(self):
        state = base_state(records=[evidence("e1", "pass")])
        for name in ("handoff_manifest", "external_evidence_manifest", "export_manifest"):
            with self.assertRaises(cli.RelayError):
                p.apply(state, {"corrections": [relation("c9", "revocation", name, "f" * 64)]},
                        BASELINE)

    def test_chunk_manifest_target_needs_a_real_resolver(self):
        state = base_state(records=[evidence("e1", "pass")])
        with self.assertRaises(cli.RelayError):
            p.apply(state, {"corrections": [relation("c9", "revocation", "chunk_manifest", "f" * 64)]},
                    BASELINE)

    def test_a_caller_supplied_digest_is_never_the_verified_value(self):
        state = base_state(records=[evidence("e1", "pass")])
        probe = copy.deepcopy(state)
        record = relation("c9", "correction", "chunk_manifest", "f" * 64)
        record["target_sha256"] = "f" * 64          # the naive echo
        probe["corrections"] = [record]
        with self.assertRaises(p.Invalid):
            p.validate(probe, targets=lambda item: "b" * 64)
        record["target_sha256"] = "b" * 64          # the derived value
        p.validate(probe, targets=lambda item: "b" * 64)

    def test_a_committed_external_target_requires_the_resolver(self):
        state = base_state(records=[evidence("e1", "pass")])
        probe = copy.deepcopy(state)
        record = relation("c9", "correction", "chunk_manifest", "f" * 64)
        record["target_sha256"] = "b" * 64
        probe["corrections"] = [record]
        with self.assertRaises(cli.RelayError):
            p.validate(probe)

    def test_a_verified_resolver_result_is_stored(self):
        state = base_state(records=[evidence("e1", "pass")])
        digest = "b" * 64
        applied = p.apply(state,
                          {"corrections": [relation("c9", "correction", "chunk_manifest", "f" * 64)]},
                          BASELINE, targets=lambda record: digest)
        saved = applied["corrections"][0]
        self.assertEqual(saved["target_sha256"], digest)
        self.assertNotEqual(saved["target_sha256"], "f" * 64)

    def test_chunked_object_records_are_not_reachable_from_a_valid_document(self):
        """Document the reason chunk-manifest targets are refused in practice.

        Every field a valid evidence record may carry is bounded, so no record
        envelope accepted by the validator can exceed the chunk threshold.
        """
        largest = {"id": "e" * 128, "task_id": "t" * 128, "check": "c" * 4096,
                   "result": "pass", "at": "2026-09-16T00:00:00Z", "ref": "r" * 4096,
                   "baseline": {"kind": "git", "fingerprint": "0" * 64},
                   "acceptance": [], "generation": 0}
        envelope = obs.envelope_text("p" * 36, "evidence", largest)
        self.assertLess(len(envelope.encode("utf-8")), obs.OBJECT_MAX_BYTES)


class ChunkManifestResolverTests(unittest.TestCase):
    """The supported manifest namespace is resolved from real bytes on disk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / ".relay").mkdir(mode=0o700, exist_ok=True)
        self.project_id = "probe-project"

    def publish_chunked(self):
        payload = {"id": "ev-big", "task_id": "t1", "check": "c", "result": "pass",
                   "at": "2026-09-16T00:00:00Z", "ref": "y" * 300000, "acceptance": ["a"]}
        entry, plans = obs.entry_for(self.root, self.project_id, "evidence", payload)
        obs.publish(self.root, plans)
        return entry

    def test_a_real_manifest_verifies(self):
        entry = self.publish_chunked()
        self.assertEqual(entry["storage"], "chunked")
        digest = relay_v4.verify_manifest_target(self.root, self.project_id,
                                                 entry["manifest_sha256"],
                                                 {entry["manifest_sha256"]})
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, entry["manifest_sha256"])

    def test_an_unreachable_manifest_is_refused(self):
        entry = self.publish_chunked()
        with self.assertRaises(cli.RelayError) as caught:
            relay_v4.verify_manifest_target(self.root, self.project_id,
                                            entry["manifest_sha256"], set())
        self.assertEqual(caught.exception.code, "RELAY_CORRECTION_TARGET_UNREACHABLE")

    def test_a_missing_manifest_is_refused(self):
        with self.assertRaises(cli.RelayError):
            relay_v4.verify_manifest_target(self.root, self.project_id, "f" * 64, {"f" * 64})

    def test_a_cross_project_manifest_is_refused(self):
        entry = self.publish_chunked()
        with self.assertRaises(cli.RelayError) as caught:
            relay_v4.verify_manifest_target(self.root, "another-project",
                                            entry["manifest_sha256"], {entry["manifest_sha256"]})
        self.assertEqual(caught.exception.code, "RELAY_OBJECT_PROJECT_MISMATCH")

    def test_a_corrupted_manifest_is_refused(self):
        entry = self.publish_chunked()
        path = obs.manifest_path(self.root, entry["manifest_sha256"])
        path.write_bytes(b'{"schema":"project-continuity/chunk-manifest/v1"}')
        with self.assertRaises(cli.RelayError):
            relay_v4.verify_manifest_target(self.root, self.project_id,
                                            entry["manifest_sha256"], {entry["manifest_sha256"]})


class _CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.assertEqual(self.call("init", "--name", "probe fixture")[0], 0)

    def call(self, *args, patch=None, expect=0):
        argv = list(args)
        if patch is not None:
            argv = argv + ["--input", "-"]
        out, err = io.StringIO(), io.StringIO()

        class _Stdin:
            def __init__(self, payload):
                self.buffer = io.BytesIO(payload)

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if patch is not None:
                with mock.patch.object(sys, "stdin", _Stdin(json.dumps(patch).encode("utf-8"))):
                    code = cli.main(argv + ["--root", str(self.root)])
            else:
                code = cli.main(argv + ["--root", str(self.root)])
        if expect is not None:
            self.assertEqual(code, expect, err.getvalue() or out.getvalue())
        body = out.getvalue().strip()
        return code, (json.loads(body) if body else {}), err.getvalue()

    def seed_v4(self):
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision", "0",
                                   "--operation-id", "r0")[0], 0)
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision", "1",
                                   "--operation-id", "s0",
                                   patch={"tasks": [{"id": "t1", "title": "t", "status": "doing",
                                                     "acceptance": ["a"]}]})[0], 0)
        source = hashlib.sha256((self.root / ".relay" / "CURRENT.md").read_bytes()).hexdigest()
        self.assertEqual(self.call("migrate", "--to-v4", "--apply", "--writer", "w",
                                   "--expected-revision", "2", "--operation-id", "m0",
                                   "--source-sha256", source)[0], 0)
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r1")[0], 0)
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision",
                                   str(revision + 1), "--operation-id", "s1",
                                   patch={"evidence": [evidence("ev-1", "pass", ("a",))]})[0], 0)

    def add_second_task(self):
        revision = self.call("status")[1]["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r2")[0], 0)
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision",
                                   str(revision + 1), "--operation-id", "s2",
                                   patch={"tasks": [{"id": "t2", "title": "second",
                                                     "status": "doing", "acceptance": ["b"]}],
                                          "evidence": [evidence("ev-2", "fail", ("b",),
                                                                task_id="t2")]})[0], 0)

    def state(self):
        return self.call("status")[1]

    def current_bytes(self):
        return len((self.root / ".relay" / "CURRENT.md").read_bytes())


class CliManifestRefusalTests(_CliCase):
    """The official write path refuses unsupported manifest targets by name."""

    def test_cli_refuses_the_ambiguous_manifest_target(self):
        self.seed_v4()
        revision = self.state()["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r2")[0], 0)
        code, _out, err = self.call("save", "--writer", "w", "--expected-revision",
                                    str(revision + 1), "--operation-id", "s2",
                                    patch={"corrections": [relation("c9", "revocation", "manifest",
                                                                    "f" * 64)]}, expect=None)
        self.assertEqual(code, 2)
        self.assertIn("RELAY_CORRECTION_TARGET_UNSUPPORTED", err)

    def test_cli_refuses_an_unreachable_chunk_manifest_target(self):
        self.seed_v4()
        revision = self.state()["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r2")[0], 0)
        code, _out, err = self.call("save", "--writer", "w", "--expected-revision",
                                    str(revision + 1), "--operation-id", "s2",
                                    patch={"corrections": [relation("c9", "revocation",
                                                                    "chunk_manifest", "f" * 64)]},
                                    expect=None)
        self.assertEqual(code, 2)
        self.assertIn("RELAY_CORRECTION_TARGET_UNREACHABLE", err)

    def test_cli_still_accepts_a_supported_evidence_target(self):
        self.seed_v4()
        state = self.state()
        target = state["evidence"][0]["id"]
        revision = state["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r2")[0], 0)
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision",
                                   str(revision + 1), "--operation-id", "s2",
                                   patch={"corrections": [relation("c9", "revocation", "evidence",
                                                                   target)]})[0], 0)
        rows = {row["text"]: row for row in self.call("coverage")[1]["rows"]}
        self.assertFalse(rows["a"]["covered"])
        gaps = self.call("handoff")[1]["unverified"]["uncovered_acceptance"]
        self.assertEqual([gap["ac_id"] for gap in gaps], [rows["a"]["ac_id"]])


class CliCoverageHandoffConsistencyTests(_CliCase):
    def revoke_first_evidence(self):
        self.seed_v4()
        state = self.state()
        revision = state["revision"]
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r2")[0], 0)
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision",
                                   str(revision + 1), "--operation-id", "s2",
                                   patch={"corrections": [relation("c9", "revocation", "evidence",
                                                                   state["evidence"][0]["id"])]})[0], 0)

    def test_revocation_is_consistent_after_a_real_round_trip(self):
        self.revoke_first_evidence()
        matrix = self.call("coverage")[1]
        handoff = self.call("handoff")[1]
        self.assertEqual(matrix["schema"], "project-continuity/coverage-matrix/v3")
        self.assertEqual(handoff["schema"], "project-continuity/handoff-view/v3")
        self.assertEqual(matrix["revision"], handoff["revision"])
        self.assertEqual(handoff["identity"]["current_sha256"], matrix["content_sha256"])
        covered = {row["ac_id"] for row in matrix["rows"] if row["covered"]}
        gaps = {gap["ac_id"] for gap in handoff["unverified"]["uncovered_acceptance"]}
        all_ids = {row["ac_id"] for row in matrix["rows"]}
        self.assertEqual(covered | gaps, all_ids)
        self.assertEqual(covered & gaps, set())
        self.assertEqual(handoff["coverage"]["uncovered"], 1)
        self.assertEqual(handoff["verification"], "unverified")
        for row in matrix["rows"]:
            self.assertTrue(row["recorded_covered"])
        status = self.call("status")[1]
        evidence_rows = {item["id"]: item for item in status["evidence"]}
        self.assertEqual(evidence_rows["ev-1"]["result"], "pass")
        self.assertEqual(evidence_rows["ev-1"]["current"], "revoked")

    def test_handoff_pages_every_list(self):
        self.seed_v4()
        self.add_second_task()
        handoff = self.call("handoff", "--limit", "1")[1]
        for name in ("tasks", "blockers", "uncovered_acceptance", "evidence"):
            self.assertIn(name, handoff["pages"], name)
            page = handoff["pages"][name]
            self.assertIn("total", page)
            self.assertIn("has_more", page)
        self.assertFalse(handoff["complete"])
        self.assertEqual(handoff["pages"]["tasks"]["has_more"], True)
        self.assertEqual(handoff["verification"], "incomplete")
        narrower = self.call("handoff", "--limit", "25", "--offset", "0")[1]
        self.assertTrue(narrower["complete"])

    def test_a_full_page_without_truncation_reports_complete(self):
        self.seed_v4()
        handoff = self.call("handoff")[1]
        self.assertTrue(handoff["complete"])
        self.assertEqual(handoff["pages"]["evidence"]["total"], 1)


class CapacityGrowthTests(_CliCase):
    def test_capacity_models_the_next_operation_receipt(self):
        self.seed_v4()
        report = self.call("capacity")[1]
        next_commit = report["next_commit"]
        self.assertFalse(next_commit["excludes_new_operation_metadata"])
        self.assertTrue(next_commit["modelled"])
        self.assertIn("operation_receipt_bytes", next_commit["model"])
        self.assertTrue(next_commit["model"]["operation_receipt_bytes"] > 0)

    def test_growth_report_covers_every_record_class(self):
        self.seed_v4()
        growth = self.call("capacity")[1]["growth"]
        for name in ("evidence", "acceptance", "task", "decision", "blocker",
                     "correction", "operation_receipt", "save_cycle"):
            self.assertIn(name, growth, name)
        for name in ("evidence", "acceptance", "task", "decision", "blocker",
                     "correction", "operation_receipt", "save_cycle"):
            item = growth[name]
            self.assertIn("current_bytes", item, name)
            self.assertIn("planned_object_bytes", item, name)
            self.assertIn("new_object_bytes", item, name)
        # the model block names the assumptions rather than pretending to be a class
        self.assertEqual(growth["model"]["status"], "modelled")
        self.assertEqual(growth["evidence"]["current_bytes"], 0,
                         "externalised evidence must not grow the document")

    def test_capacity_preview_matches_the_committed_bytes(self):
        self.seed_v4()
        revision = self.state()["revision"]
        preview = self.call("capacity", "--writer", "w", "--expected-revision",
                            str(revision), "--operation-id", "r-preview")[1]
        self.assertFalse(preview["next_commit"]["estimate_only"])
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                   str(revision), "--operation-id", "r-preview")[0], 0)
        self.assertEqual(preview["next_commit"]["candidate_bytes"], self.current_bytes())

    def test_repeated_evidence_growth_stays_within_the_v4_cap(self):
        self.seed_v4()
        sizes = [self.current_bytes()]
        for step in range(1, 6):
            revision = self.state()["revision"]
            self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision",
                                       str(revision), "--operation-id", "gr" + str(step))[0], 0)
            self.assertEqual(self.call("save", "--writer", "w", "--expected-revision",
                                       str(revision + 1), "--operation-id", "gs" + str(step),
                                       patch={"evidence": [evidence("ev-g" + str(step), "pass",
                                                                    ("a",))]})[0], 0)
            sizes.append(self.current_bytes())
        self.assertLessEqual(max(sizes), p.V4_MAX_BYTES)


if __name__ == "__main__":
    unittest.main()
