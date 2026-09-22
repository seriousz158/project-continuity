"""Deep object integrity for handoff/coverage --verify-integrity.

Round "relay-v4 capacity/optimization": the installed 0.3.3 raised
NameError from cli_v2.integrity_check because it called an undefined
object_integrity.  These cases pin the repaired contract:

  * every reachable object is actually read (a real deep check);
  * missing / tampered / wrong-type / cross-project objects are named;
  * an exhausted validation budget is INCOMPLETE, never a pass;
  * a legacy document reports integrity as not applicable, not as a pass;
  * pagination that is incomplete can never produce a verified conclusion;
  * the source tree has no undefined globals, duplicate top-level
    definitions or shadowed re-implementations.

Every case runs in an isolated temporary project.  No live relay, worktree or
real project repository is read or written.
"""
from __future__ import annotations

import ast
import builtins
import contextlib
import io
import json
import os
import subprocess
import symtable
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cli_v2 as cli              # noqa: E402
import objectstore as obs         # noqa: E402
import progress as p              # noqa: E402
import relay_errors as errors     # noqa: E402
import v4 as relay_v4             # noqa: E402

WRITE = SCRIPTS / "write_current.py"
INSTALLED = Path(os.environ.get(
    "PC_INSTALLED_SKILL",
    str(Path.home() / ".cc-switch" / "skills" / "project-continuity")))

BUDGET_CODE = errors.RELAY_VALIDATION_BUDGET_EXCEEDED


class _FakeStdin:
    def __init__(self, payload: bytes):
        self.buffer = io.BytesIO(payload)


class IntegrityCase(unittest.TestCase):
    """A v4 fixture whose object store can be tampered with."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.assertEqual(self.call("init", "--name", "integrity fixture")[0], 0)

    def call(self, *args, patch=None, expect=0, script=None):
        argv = [str(script or WRITE), *args, "--root", str(self.root)]
        if patch is not None:
            argv = argv + ["--input", "-"]
        result = subprocess.run(
            [sys.executable, "-B", *argv],
            input=json.dumps(patch) if patch is not None else None,
            text=True, encoding="utf-8", capture_output=True, timeout=120,
            cwd=str(SKILL))
        body = result.stdout.strip()
        parsed = json.loads(body) if body else {}
        if expect is not None:
            self.assertEqual(result.returncode, expect, result.stderr or result.stdout)
        return result.returncode, parsed, result.stderr

    # -- fixture ---------------------------------------------------------
    def named_refusal(self, *args):
        """A corrupted store is refused by name, not rendered as a partial view."""
        before = self.current()
        code, _out, err = self.call(*args, expect=2)
        self.assertEqual(self.current(), before, "a refused read changed CURRENT.md")
        return json.loads(err)

    def seed(self, acceptance=("condition one", "condition two"), evidence=True, v4=True):
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision", "0",
                                   "--operation-id", "resume-1")[0], 0)
        patch = {"tasks": [{"id": "t1", "title": "fixture task", "status": "doing",
                            "acceptance": list(acceptance)}]}
        if evidence:
            patch["evidence"] = [{"id": "ev-1", "task_id": "t1", "check": "check ev-1",
                                  "result": "pass", "at": "2026-09-16T00:00:00Z",
                                  "ref": "runs/ev-1", "acceptance": ["condition one"]}]
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision", "1",
                                   "--operation-id", "save-1", patch=patch)[0], 0)
        if v4:
            self.to_v4()

    def to_v4(self):
        preview = self.call("migrate", "--to-v4")[1]
        revision = self.call("status")[1]["revision"]
        result = self.call("migrate", "--to-v4", "--apply", "--writer", "w",
                           "--expected-revision", str(revision),
                           "--operation-id", "migrate-v4",
                           "--source-sha256", preview["source_sha256"])[1]
        self.assertEqual(result["schema"], p.SCHEMA_V4)
        return result

    def to_v3(self):
        preview = self.call("migrate", "--to-v3")[1]
        revision = self.call("status")[1]["revision"]
        result = self.call("migrate", "--to-v3", "--apply", "--writer", "w",
                           "--expected-revision", str(revision),
                           "--operation-id", "migrate-v3",
                           "--source-sha256", preview["source_sha256"])[1]
        self.assertEqual(result["schema"], p.SCHEMA_V3)
        return result

    def current(self):
        return (self.root / ".relay" / "CURRENT.md").read_text(encoding="utf-8")

    def write_current(self, text):
        (self.root / ".relay" / "CURRENT.md").write_text(text, encoding="utf-8")

    def doc_and_stub(self):
        text = self.current()
        meta, body = relay_v4.parse_envelope(text)
        state, _matches = p.parse_body(body)
        return text, meta, state

    # -- tampering (fixture only; never used on a real relay) -------------
    def object_files(self, record_type="evidence"):
        base = self.root / ".relay" / "objects" / record_type
        return sorted(base.rglob("*.json")) if base.exists() else []

    def rewrite_first_object(self, mutate, record_type="evidence", field="evidence"):
        """Replace the first object with a re-hashed, mutated envelope.

        The new object is stored under its own digest and the reachable index
        node is republished, so the only defect left is the mutation itself.
        """
        text, meta, stub = self.doc_and_stub()
        ref = stub[field]
        entries = obs.read_index(self.root, meta["project_id"], record_type,
                                 ref["index"], obs.Budget())
        target = dict(entries[0])
        path = obs.object_path(self.root, record_type, target["object_sha256"])
        envelope = json.loads(path.read_bytes().decode("utf-8"))
        mutate(envelope)
        data = obs.canonical(envelope).encode("utf-8")
        new_sha = obs.sha256_hex(data)
        new_path = obs.object_path(self.root, record_type, new_sha)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_bytes(data)
        target["object_sha256"] = new_sha
        target["object_bytes"] = len(data)
        entries[0] = target
        plans, new_index = obs.plan_index(self.root, meta["project_id"], record_type, entries)
        for plan in plans:
            plan["path"].parent.mkdir(parents=True, exist_ok=True)
            plan["path"].write_bytes(plan["content"])
        self.assertIn(ref["index"], text)
        self.write_current(text.replace(ref["index"], new_index, 1))

    def delete_first_object(self, record_type="evidence", field="evidence"):
        text, meta, stub = self.doc_and_stub()
        entries = obs.read_index(self.root, meta["project_id"], record_type,
                                 stub[field]["index"], obs.Budget())
        victim = obs.object_path(self.root, record_type, entries[0]["object_sha256"])
        victim.unlink()
        return victim

    def integrity_of(self, payload):
        return payload["verification_record"]["checks"]["integrity"]

    # -- assertions ------------------------------------------------------
    def assert_not_verified(self, payload, named):
        record = payload["verification_record"]
        self.assertFalse(record["verified"], record)
        self.assertIn(named, json.dumps(record["checks"]["integrity"]))
        self.assertNotEqual(record["checks"]["integrity"]["state"], "verified")


class DeepIntegrityCliTests(IntegrityCase):
    def test_handoff_verify_integrity_reads_every_reachable_object(self):
        self.seed()
        code, out, err = self.call("handoff", "--limit", "5", "--verify-integrity")
        self.assertEqual(code, 0, err)
        check = self.integrity_of(out)
        self.assertTrue(check["checked"], check)
        self.assertEqual(check["state"], "verified", check)
        self.assertTrue(check["verified"], check)
        self.assertGreater(check["objects_checked"], 0, check)
        record = out["verification_record"]
        self.assertIn("integrity", record["required"], record)
        self.assertTrue(record["scope"]["integrity_check_executed"], record)

    def test_coverage_verify_integrity_reports_the_same_check(self):
        self.seed()
        code, out, err = self.call("coverage", "--limit", "5", "--verify-integrity")
        self.assertEqual(code, 0, err)
        check = self.integrity_of(out)
        self.assertEqual(check["state"], "verified", check)
        self.assertGreater(check["objects_checked"], 0, check)

    def test_missing_object_is_named_corruption(self):
        self.seed()
        self.delete_first_object()
        refusal = self.named_refusal("handoff", "--limit", "5", "--verify-integrity")
        self.assertEqual(refusal["error"], errors.RELAY_OBJECT_MISSING, refusal)

    def test_tampered_object_is_a_hash_mismatch(self):
        self.seed()
        victim = self.object_files()[0]
        victim.write_bytes(victim.read_bytes() + b" ")
        refusal = self.named_refusal("handoff", "--limit", "5", "--verify-integrity")
        self.assertEqual(refusal["error"], errors.RELAY_OBJECT_HASH_MISMATCH, refusal)

    def test_wrong_object_type_is_refused(self):
        self.seed()
        self.rewrite_first_object(lambda env: env.update({"record_type": "correction"}))
        refusal = self.named_refusal("handoff", "--limit", "5", "--verify-integrity")
        self.assertEqual(refusal["error"], errors.RELAY_OBJECT_SCHEMA_INVALID, refusal)
        self.assertIn("type or format", refusal["message"])

    def test_cross_project_object_is_refused(self):
        self.seed()
        self.rewrite_first_object(lambda env: env.update({"project_id": "some-other-project"}))
        refusal = self.named_refusal("handoff", "--limit", "5", "--verify-integrity")
        self.assertEqual(refusal["error"], errors.RELAY_OBJECT_PROJECT_MISMATCH, refusal)

    def test_coverage_refuses_a_corrupted_store_by_name(self):
        self.seed()
        self.delete_first_object()
        refusal = self.named_refusal("coverage", "--limit", "5", "--verify-integrity")
        self.assertEqual(refusal["error"], errors.RELAY_OBJECT_MISSING, refusal)

    def test_incomplete_pagination_is_never_verified(self):
        self.seed()
        code, out, err = self.call("handoff", "--limit", "5", "--verify-integrity",
                                   "--tasks-offset", "0", "--uncovered-limit", "1")
        self.assertEqual(code, 0, err)
        record = out["verification_record"]
        if not record["complete"]:
            self.assertFalse(record["verified"], record)
            self.assertEqual(record["reason"], "pagination_incomplete", record)
        else:
            self.assertTrue(out["pages"]["uncovered_acceptance"]["total"] >= 1)

    def test_legacy_document_reports_integrity_not_applicable(self):
        self.seed(v4=False)
        self.to_v3()
        code, out, err = self.call("handoff", "--limit", "5", "--verify-integrity")
        self.assertEqual(code, 0, err)
        check = self.integrity_of(out)
        self.assertFalse(check["applicable"], check)
        self.assertNotEqual(check["state"], "verified", check)
        self.assertFalse(check["verified"], check)
        self.assertNotIn("integrity", out["verification_record"]["required"])

    def test_verify_integrity_flag_is_optional(self):
        self.seed()
        code, out, err = self.call("handoff", "--limit", "5")
        self.assertEqual(code, 0, err)
        check = self.integrity_of(out)
        self.assertFalse(check["checked"], check)
        self.assertEqual(check["state"], "not_checked", check)


class DeepIntegrityLibraryTests(IntegrityCase):
    def test_exhausted_budget_is_incomplete_and_never_a_pass(self):
        self.seed()
        _text, meta, stub = self.doc_and_stub()
        result = relay_v4.object_integrity(self.root, meta["project_id"], stub,
                                           meta["schema"], budget=obs.Budget(max_objects=1))
        self.assertEqual(result["state"], "budget_exceeded", result)
        self.assertEqual(result["reason"], BUDGET_CODE, result)
        self.assertFalse(result["verified"], result)
        self.assertNotEqual(result["state"], "verified")
        check = relay_v4._integrity_check(result)
        self.assertFalse(check["verified"], check)
        self.assertTrue(check["applicable"], check)

    def test_budget_exhaustion_blocks_a_handoff_conclusion(self):
        self.seed()
        _text, meta, stub = self.doc_and_stub()
        degraded = {"checked": True, "state": "budget_exceeded", "reason": BUDGET_CODE,
                    "applicable": True, "objects_checked": None}
        state, _meta2, _stub2 = self.doc_and_stub()
        _t, body = relay_v4.parse_envelope(self.current())
        full = relay_v4.expand(stub, self.root, meta["project_id"])
        checks = relay_v4._verification_checks(full, None, [], None, degraded, {}, None)
        record = relay_v4._verification_record(checks)
        self.assertFalse(record["verified"], record)
        self.assertIn("integrity", record["required"], record)

    def test_object_integrity_reports_applicable_schema(self):
        self.seed()
        _text, meta, stub = self.doc_and_stub()
        result = relay_v4.object_integrity(self.root, meta["project_id"], stub, meta["schema"])
        self.assertEqual(result["state"], "verified", result)
        self.assertTrue(result["applicable"], result)

    def test_object_integrity_is_named_on_a_legacy_schema(self):
        self.seed(v4=False)
        _text, meta, stub = self.doc_and_stub()
        result = relay_v4.object_integrity(self.root, meta["project_id"], stub, meta["schema"])
        self.assertFalse(result["applicable"], result)
        self.assertEqual(result["state"], "not_applicable", result)
        self.assertNotEqual(result["state"], "verified", result)
        self.assertFalse(result["verified"], result)
        self.assertEqual(result["objects_checked"], 0, result)

    def test_object_integrity_names_corruption_and_never_passes_it(self):
        self.seed()
        self.delete_first_object()
        _text, meta, stub = self.doc_and_stub()
        result = relay_v4.object_integrity(self.root, meta["project_id"], stub, meta["schema"])
        self.assertEqual(result["state"], "degraded", result)
        self.assertEqual(result["reason"], errors.RELAY_OBJECT_MISSING, result)
        self.assertFalse(result["verified"], result)
        check = relay_v4._integrity_check(result)
        self.assertTrue(check["applicable"], check)
        self.assertFalse(check["verified"], check)

    def test_object_integrity_detects_an_ac_map_rebinding(self):
        self.seed()
        text, meta, stub = self.doc_and_stub()
        rebound = json.loads(json.dumps(stub["extensions"]["ac_map"]))
        rebound["entries"][0]["sha256"] = "0" * 64
        _lines, body, _meta = cli.split(text)
        state, _matches = p.parse_body(body)
        state["extensions"]["ac_map"] = rebound
        new_text = text.replace(body, p.render_body(state, body), 1)
        self.write_current(new_text)
        _t, meta2, stub2 = self.doc_and_stub()
        result = relay_v4.object_integrity(self.root, meta2["project_id"], stub2, meta2["schema"])
        self.assertEqual(result["state"], "degraded", result)
        self.assertEqual(result["reason"], errors.RELAY_AC_MAP_MISMATCH, result)
        self.assertFalse(result["verified"], result)


class SourceIntegrityTests(unittest.TestCase):
    """The class of defect that produced the NameError, checked mechanically."""

    @staticmethod
    def modules():
        return sorted(SCRIPTS.glob("*.py"))

    def test_no_references_to_undefined_globals(self):
        offenders = {}
        for path in self.modules():
            source = path.read_text(encoding="utf-8")
            table = symtable.symtable(source, str(path), "exec")
            known = {symbol.get_name() for symbol in table.get_symbols()} | set(dir(builtins))
            missing = set()

            def visit(node):
                for symbol in node.get_symbols():
                    name = symbol.get_name()
                    implicit = name.startswith("__") and name.endswith("__")
                    if symbol.is_referenced() and not symbol.is_assigned() \
                            and not symbol.is_parameter() and symbol.is_global() \
                            and name not in known and not implicit:
                        missing.add(name)
                for child in node.get_children():
                    visit(child)

            visit(table)
            if missing:
                offenders[path.name] = sorted(missing)
        self.assertEqual(offenders, {}, offenders)

    def test_no_duplicate_top_level_definitions(self):
        offenders = {}
        for path in self.modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            seen = {}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                        and node.col_offset == 0:
                    seen.setdefault(node.name, []).append(node.lineno)
            duplicates = {name: lines for name, lines in seen.items() if len(lines) > 1}
            if duplicates:
                offenders[path.name] = duplicates
        self.assertEqual(offenders, {}, offenders)

    def test_integrity_entry_points_are_wired_to_a_defined_check(self):
        source = (SCRIPTS / "cli_v2.py").read_text(encoding="utf-8")
        self.assertIn("def integrity_check(", source)
        self.assertIn("object_integrity(", source)
        self.assertTrue(hasattr(relay_v4, "object_integrity"),
                        "v4.object_integrity is not defined")
        self.assertTrue(callable(getattr(relay_v4, "object_integrity")))
        for name in ("handoff", "coverage"):
            self.assertIn(name, source)


@unittest.skipUnless((INSTALLED / "scripts" / "write_current.py").is_file(),
                     "no installed project-continuity skill on this host")
class InstalledCliTests(IntegrityCase):
    """The deployed entry point must run the same deep check."""

    def test_installed_cli_runs_the_deep_check(self):
        script = INSTALLED / "scripts" / "write_current.py"
        self.seed()
        code, out, err = self.call("handoff", "--limit", "5", "--verify-integrity",
                                   script=script)
        self.assertEqual(code, 0, err)
        check = self.integrity_of(out)
        self.assertEqual(check["state"], "verified", check)
        self.assertGreater(check["objects_checked"], 0, check)


if __name__ == "__main__":
    unittest.main()
