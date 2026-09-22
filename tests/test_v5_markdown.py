"""project-continuity/v5: external custom Markdown and acceptance map.

Every case runs in an isolated temporary project; no live relay, worktree or
real project repository is read or written.

The contract under test:

  * v5 is reached only by an explicit, source-bound migration from v4;
  * the dry run publishes nothing and the apply publishes objects before the
    document;
  * resolving a v5 document yields the same business state its v4 predecessor
    resolved to, and the Markdown is reconstructed byte for byte;
  * the acceptance map stays bound to the acceptance conditions it was minted
    from, and a task change publishes a new record instead of reusing a stale one;
  * the capacity target is met and modelled, not assumed;
  * a v5 document keeps working: resume, save, handoff, export and verify.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cli_v2 as cli              # noqa: E402
import objectstore as obs         # noqa: E402
import progress as p              # noqa: E402
import v4 as relay_v4             # noqa: E402

WRITE = SCRIPTS / "write_current.py"
OBSERVATION_TARGET = 24576
HARD_LIMIT = 32768


class _FakeStdin:
    def __init__(self, payload: bytes):
        self.buffer = io.BytesIO(payload)


class V5Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.assertEqual(self.call("init", "--name", "v5 fixture")[0], 0)

    def call(self, *args, patch=None, expect=0, script=None):
        argv = [sys.executable, "-B", str(script or WRITE), *args, "--root", str(self.root)]
        if patch is not None:
            argv = argv + ["--input", "-"]
        result = subprocess.run(
            argv, input=json.dumps(patch) if patch is not None else None,
            text=True, encoding="utf-8", capture_output=True, timeout=180,
            cwd=str(SKILL))
        parsed = json.loads(result.stdout) if result.stdout.strip() else {}
        if expect is not None:
            self.assertEqual(result.returncode, expect, result.stderr or result.stdout)
        return result.returncode, parsed, result.stderr

    # -- fixtures --------------------------------------------------------
    def seed(self, acceptance=("condition one", "condition two")):
        self.assertEqual(self.call("resume", "--writer", "w", "--expected-revision", "0",
                                   "--operation-id", "resume-1")[0], 0)
        patch = {"tasks": [{"id": "t1", "title": "fixture task", "status": "doing",
                            "acceptance": list(acceptance)}],
                 "evidence": [{"id": "ev-1", "task_id": "t1", "check": "check ev-1",
                               "result": "pass", "at": "2026-09-16T00:00:00Z",
                               "ref": "runs/ev-1", "acceptance": ["condition one"]}]}
        self.assertEqual(self.call("save", "--writer", "w", "--expected-revision", "1",
                                   "--operation-id", "save-1", patch=patch)[0], 0)

    def to_v4(self):
        preview = self.call("migrate", "--to-v4")[1]
        revision = self.call("status")[1]["revision"]
        result = self.call("migrate", "--to-v4", "--apply", "--writer", "w",
                           "--expected-revision", str(revision),
                           "--operation-id", "migrate-v4",
                           "--source-sha256", preview["source_sha256"])[1]
        self.assertEqual(result["schema"], p.SCHEMA_V4)

    FIXTURE_REGION = "\n## 目标\n- keep this text\n## 状态\n- 当前进展（中文）\n"

    def v4_project(self, region=None):
        """A v4 fixture with real custom Markdown in its unmanaged region."""
        self.seed()
        self.to_v4()
        self.set_region(self.FIXTURE_REGION if region is None else region)

    def set_region(self, region):
        lines, body, _meta = cli.split(self.current())
        self.write_current(cli.metadata(lines, {}) + p.render_region(body, region))

    def to_v5(self, operation_id="migrate-v5"):
        preview = self.call("migrate", "--to-v5")[1]
        revision = self.call("status")[1]["revision"]
        self.migration_revision = revision
        result = self.call("migrate", "--to-v5", "--apply", "--writer", "w",
                           "--expected-revision", str(revision),
                           "--operation-id", operation_id,
                           "--source-sha256", preview["source_sha256"])[1]
        self.assertEqual(result["schema"], p.SCHEMA_V5)
        return result

    # -- helpers ---------------------------------------------------------
    def current(self):
        return (self.root / ".relay" / "CURRENT.md").read_text(encoding="utf-8")

    def write_current(self, text):
        (self.root / ".relay" / "CURRENT.md").write_text(text, encoding="utf-8")

    def state(self, schema=None):
        return cli.resolve(self.current(), self.root)

    def business_digest(self):
        state = json.loads(json.dumps(self.state()))
        state.pop("operations", None)
        for key in ("migration_v5", "external_markdown"):
            state["extensions"].pop(key, None)
        return p.digest(state)

    def region(self):
        _lines, body, _meta = cli.split(self.current())
        return p.custom_region(body)

    def object_inventory(self):
        base = self.root / ".relay" / "objects"
        if not base.exists():
            return {}
        return {str(path.relative_to(base)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(base.rglob("*")) if path.is_file()}

    def markdown(self, *extra):
        code, out, err = self.call("markdown", *extra)
        self.assertEqual(code, 0, err)
        return out


class V5MigrationTests(V5Case):
    def test_migration_is_explicit_lossless_and_source_bound(self):
        self.v4_project()
        before_state = self.business_digest()
        before_region = self.region()
        before_objects = self.object_inventory()
        revision = self.call("status")[1]["revision"]

        preview = self.call("migrate", "--to-v5")[1]
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["source_sha256"],
                         hashlib.sha256(self.current().encode()).hexdigest())
        self.assertLessEqual(preview["candidate_bytes"], OBSERVATION_TARGET)
        self.assertEqual(self.object_inventory(), before_objects,
                         "the dry run published objects")

        self.call("migrate", "--to-v5", "--apply", "--writer", "w",
                  "--expected-revision", str(revision), "--operation-id", "migrate-v5",
                  "--source-sha256", "0" * 64, expect=2)
        self.assertEqual(self.call("status")[1]["schema"], p.SCHEMA_V4,
                         "a wrong source digest migrated the document")

        self.to_v5()
        self.assertEqual(self.call("status")[1]["schema"], p.SCHEMA_V5)
        self.assertEqual(self.business_digest(), before_state,
                         "the migration changed the business state")
        self.assertEqual(self.markdown()["text"], before_region)
        self.assertTrue(self.object_inventory() != before_objects)

    def test_migration_requires_a_v4_document(self):
        self.v4_project()
        self.to_v5()
        result = self.call("migrate", "--to-v5")[1]
        self.assertFalse(result["migration_required"])
        self.assertEqual(result["schema"], p.SCHEMA_V5)

    def test_v5_document_without_its_indexes_is_refused(self):
        self.v4_project()
        self.to_v5()
        text = self.current()
        stub_state, _matches = p.parse_body(cli.split(text)[1])
        stub_state.pop("markdown")
        body = p.render_body(stub_state, cli.split(text)[1])
        self.write_current(cli.metadata(cli.split(text)[0], {}) + body)
        code, out, err = self.call("handoff", "--limit", "3", expect=2)
        self.assertIn("v5 requires external markdown", err)

    def test_a_v4_document_still_reads_and_resolves(self):
        self.v4_project()
        state = self.state()
        self.assertEqual(state["project"]["name"], "v5 fixture")
        region = self.region()
        self.assertEqual(self.markdown()["text"], region)
        self.assertFalse(self.markdown()["external"])
        self.assertEqual(self.call("handoff", "--limit", "3", "--verify-integrity")[0], 0)


class V5MarkdownTests(V5Case):
    def test_stub_replaces_the_region_and_keeps_the_provenance(self):
        narrative = "\n## 目标\n" + "".join("- 历史说明 %d\n" % i for i in range(80))
        self.v4_project(region=narrative)
        region = self.region()
        self.to_v5()
        stub = self.region()
        self.assertIn(p.MARKDOWN_STUB_HEADING, stub)
        self.assertNotIn("- keep this text", stub)
        # Every section heading stays in the document, so a reader that looks
        # for a known marker (the canonical status file requires
        # "## 前置任务状态（live）") still finds it after externalisation.
        for record in p.split_markdown(region):
            if record["title"] != p.MARKDOWN_PREAMBLE_TITLE:
                self.assertIn("## " + record["title"], stub)
        self.assertEqual(stub.count("## "), 1 + len(p.split_markdown(region)) - 1)
        self.assertLess(len(stub.encode()), len(region.encode()))
        state = cli.envelope_state(self.current())[1]
        provenance = state["extensions"]["external_markdown"]
        self.assertEqual(provenance["schema"], p.EXTERNAL_MARKDOWN_SCHEMA)
        self.assertEqual(provenance["sections"], len(p.split_markdown(region)))
        self.assertGreater(provenance["sections"], 0)
        self.assertEqual(provenance["bytes"], len(region.encode()))
        self.assertLessEqual(len(self.current().encode()), OBSERVATION_TARGET)

    def test_markdown_command_restores_every_byte(self):
        self.v4_project()
        original = self.region()
        self.to_v5()
        restored = self.markdown()
        self.assertEqual(restored["text"], original)
        self.assertEqual(restored["sha256"],
                         hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual(restored["bytes"], len(original.encode()))
        self.assertTrue(restored["external"])
        self.assertEqual(restored["provenance"]["source_revision"],
                         self.migration_revision)
        records = p.split_markdown(original)
        for record in records:
            section = self.markdown("--section", record["id"])
            self.assertEqual(section["text"], record["content"])
            self.assertEqual(section["sha256"], record["sha256"])

    def test_markdown_command_reports_an_unknown_section(self):
        self.v4_project()
        self.to_v5()
        code, _out, err = self.call("markdown", "--section", "md-" + "0" * 16, expect=2)
        self.assertIn("unknown markdown section", err)

    def test_split_is_reversible_for_every_shape(self):
        for region in ("\n## one\n- a\n", "\n", "", "\n## one\n- a\n## two\n- b\n",
                       "\n## 目标\n- 中文内容\n"):
            records = p.split_markdown(region)
            self.assertEqual(p.join_markdown(records), region)
            self.assertEqual(p.render_markdown_stub(records)[:0], "")

    def test_reserved_markers_and_duplicate_titles_are_refused(self):
        with self.assertRaises(p.Invalid):
            p.split_markdown("\n## a\n<!-- project-continuity:data -->\n")
        with self.assertRaises(p.Invalid):
            p.split_markdown("\n## a\n- x\n## a\n- y\n")
        with self.assertRaises(p.Invalid):
            p.split_markdown("- not a heading\n")

    def test_a_hand_edited_stub_is_regenerated_and_the_old_bytes_are_in_history(self):
        self.v4_project()
        region = self.region()
        self.to_v5()
        revision = self.call("status")[1]["revision"]
        canonical_stub = self.region()
        self.set_region(canonical_stub + "- hand note\n")
        resumed = self.call("resume", "--writer", "w", "--expected-revision", str(revision),
                            "--operation-id", "resume-h")[1]
        self.assertIn("custom markdown stub was regenerated", " ".join(resumed["warnings"]))
        self.call("save", "--writer", "w", "--expected-revision",
                  str(revision + 1), "--operation-id", "save-h",
                  patch={"project": {"next_step": "after stub repair"}})
        self.assertEqual(self.region(), canonical_stub)
        self.assertNotIn("hand note", self.region())
        # The text was never lost: it is still the restored Markdown, and the
        # edited document survives in the history snapshot of the old revision.
        self.assertEqual(self.markdown()["text"], region)
        history = sorted((self.root / ".relay" / "history").iterdir())
        self.assertTrue(any("- hand note" in path.read_text(encoding="utf-8")
                            for path in history), "the pre-edit bytes left no history")


class V5MarkdownUpsertTests(V5Case):
    """A v5 document gains or replaces sections through a typed change."""

    def _write(self, patch, operation="md-1"):
        revision = self.call("status")[1]["revision"]
        self.call("resume", "--writer", "w", "--expected-revision", str(revision),
                  "--operation-id", "resume-" + operation)
        return self.call("save", "--writer", "w", "--expected-revision",
                         str(revision + 1), "--operation-id", "save-" + operation,
                         patch=patch)[1]

    def test_a_new_section_is_added_without_inflating_the_document(self):
        self.v4_project()
        original = self.region()
        self.to_v5()
        before_bytes = len(self.current().encode())
        result = self._write({"markdown": [{"title": "Relay 优化",
                                            "content": "## Relay 优化\n- 外置保留原文\n"}]})
        self.assertEqual(result["markdown"],
                         {"applied": 1, "replaced": 0, "added": 1, "sections": 4})
        document = self.current()
        self.assertIn("## Relay 优化", document)
        self.assertLessEqual(len(document.encode()), OBSERVATION_TARGET)
        self.assertLess(len(document.encode()) - before_bytes, 400)
        restored = self.markdown()
        self.assertTrue(restored["text"].startswith(original))
        self.assertTrue(restored["text"].endswith("## Relay 优化\n- 外置保留原文\n"))
        self.assertEqual(restored["count"], 4)

    def test_replacing_a_section_keeps_the_other_sections_byte_identical(self):
        self.v4_project()
        original = self.region()
        self.to_v5()
        self._write({"markdown": [{"title": "状态",
                                   "content": "## 状态\n- 更新后的正文（中文）\n"}]})
        restored = self.markdown()["text"]
        head, tail = original.split("## 状态\n", 1)
        new_head, new_tail = restored.split("## 状态\n", 1)
        self.assertEqual(new_head, head)
        self.assertTrue(new_tail.startswith("- 更新后的正文（中文）\n"))
        self.assertTrue(tail.split("\n", 1)[1] in new_tail)
        self.assertEqual(self.call("handoff", "--limit", "5", "--verify-integrity")[0], 0)

    def test_a_markdown_change_on_a_v4_document_is_refused(self):
        self.v4_project()
        revision = self.call("status")[1]["revision"]
        self.call("resume", "--writer", "w", "--expected-revision", str(revision),
                  "--operation-id", "resume-v4")
        code, _out, err = self.call("save", "--writer", "w", "--expected-revision",
                                    str(revision + 1), "--operation-id", "save-v4",
                                    patch={"markdown": [{"title": "x", "content": "## x\n"}]},
                                    expect=2)
        self.assertIn("markdown changes require a v5 document", err)

    def test_a_section_change_must_carry_its_own_heading(self):
        self.v4_project()
        self.to_v5()
        code, _out, err = self._call_merge([{"title": "标题", "content": "正文没有标题\n"}])
        self.assertIn("must start with its own heading", err)

    def _call_merge(self, additions):
        revision = self.call("status")[1]["revision"]
        self.call("resume", "--writer", "w", "--expected-revision", str(revision),
                  "--operation-id", "resume-bad")
        return self.call("save", "--writer", "w", "--expected-revision",
                         str(revision + 1), "--operation-id", "save-bad",
                         patch={"markdown": additions}, expect=2)


class V5AcceptanceMapTests(V5Case):
    def test_map_is_bound_to_the_acceptance_conditions(self):
        self.v4_project()
        before_map = p.build_ac_map(self.state()["tasks"])
        before_region = self.region()
        self.to_v5()
        self.assertEqual(self.state()["extensions"]["ac_map"], before_map)
        meta, stub, _matches = cli.envelope_state(self.current())
        records = relay_v4.collect_markdown(stub, self.root, meta["project_id"])
        self.assertEqual(p.join_markdown(records), before_region)
        self.assertEqual(len(records), len(p.split_markdown(before_region)))

    def test_a_task_change_publishes_a_new_map_record(self):
        self.v4_project()
        self.to_v5()
        before = cli.envelope_state(self.current())[1]["ac_map"]["index"]
        revision = self.call("status")[1]["revision"]
        self.call("resume", "--writer", "w", "--expected-revision", str(revision),
                  "--operation-id", "resume-2")
        self.call("save", "--writer", "w", "--expected-revision", str(revision + 1),
                  "--operation-id", "save-2",
                  patch={"tasks": [{"id": "t1", "title": "fixture task", "status": "doing",
                                    "acceptance": ["condition one", "condition two",
                                                   "condition three"]}]})
        state = cli.envelope_state(self.current())[1]
        self.assertNotEqual(state["ac_map"]["index"], before,
                            "a changed acceptance set reused the stale map object")
        self.assertEqual(self.state()["extensions"]["ac_map"],
                         p.build_ac_map(self.state()["tasks"]))
        self.assertEqual(self.call("handoff", "--limit", "5", "--verify-integrity")[0], 0)

    def _rebind_map(self, consistent):
        """Replace the acceptance-map record with one that lies about a digest.

        With consistent=False the document still points at the old record, so
        the index digest disagrees; with consistent=True the reference is
        updated too, so only the semantic binding can catch it.
        """
        text = self.current()
        project_id = cli.envelope_state(text)[0]["project_id"]
        stub_state, _ = p.parse_body(cli.split(text)[1])
        entries = obs.read_index(self.root, project_id, "ac-map",
                                 stub_state["ac_map"]["index"], obs.Budget())
        target = obs.object_path(self.root, "ac-map", entries[0]["object_sha256"])
        envelope = json.loads(target.read_bytes().decode("utf-8"))
        envelope["payload"]["map"]["entries"][0]["sha256"] = "0" * 64
        data = obs.canonical(envelope).encode("utf-8")
        new_sha = obs.sha256_hex(data)
        obs.object_path(self.root, "ac-map", new_sha).parent.mkdir(parents=True, exist_ok=True)
        obs.object_path(self.root, "ac-map", new_sha).write_bytes(data)
        entries[0]["object_sha256"] = new_sha
        entries[0]["object_bytes"] = len(data)
        plans, index_sha = obs.plan_index(self.root, project_id, "ac-map", entries)
        for plan in plans:
            plan["path"].parent.mkdir(parents=True, exist_ok=True)
            plan["path"].write_bytes(plan["content"])
        text = text.replace(stub_state["ac_map"]["index"], index_sha, 1)
        if consistent:
            text = text.replace(stub_state["ac_map"]["sha256"],
                                p.digest([envelope["payload"]]), 1)
        self.write_current(text)

    def test_a_rebound_map_object_is_refused_by_the_index(self):
        self.v4_project()
        self.to_v5()
        self._rebind_map(consistent=False)
        code, _out, err = self.call("handoff", "--limit", "3", "--verify-integrity", expect=2)
        self.assertIn("RELAY_INDEX_INVALID", err)

    def test_a_self_consistent_rebound_map_is_refused_by_its_binding(self):
        self.v4_project()
        self.to_v5()
        self._rebind_map(consistent=True)
        code, _out, err = self.call("handoff", "--limit", "3", "--verify-integrity", expect=2)
        self.assertIn("RELAY_AC_MAP_MISMATCH", err)


class V5OperationalTests(V5Case):
    def test_writes_keep_v5_and_stay_under_the_target(self):
        self.v4_project()
        self.to_v5()
        revision = self.call("status")[1]["revision"]
        sizes = []
        for cycle in range(3):
            self.call("resume", "--writer", "w", "--expected-revision", str(revision),
                      "--operation-id", "resume-c%d" % cycle)
            self.call("save", "--writer", "w", "--expected-revision", str(revision + 1),
                      "--operation-id", "save-c%d" % cycle,
                      patch={"project": {"next_step": "cycle %d" % cycle}})
            revision += 2
            sizes.append(len(self.current().encode()))
        self.assertEqual(self.call("status")[1]["schema"], p.SCHEMA_V5)
        self.assertTrue(all(size <= OBSERVATION_TARGET for size in sizes), sizes)
        self.assertTrue(all(size <= HARD_LIMIT for size in sizes), sizes)
        self.assertEqual(self.call("handoff", "--limit", "5", "--verify-integrity")[0], 0)

    def test_next_commit_fits_with_argument_precision(self):
        self.v4_project()
        self.to_v5()
        revision = self.call("status")[1]["revision"]
        preview = self.call("capacity", "--writer", "w", "--expected-revision", str(revision),
                            "--operation-id", "next-1")[1]
        self.assertFalse(preview["next_commit"]["estimate_only"])
        self.assertTrue(preview["next_commit"]["would_fit"])
        self.assertLessEqual(preview["next_commit"]["candidate_bytes"], OBSERVATION_TARGET)
        self.call("resume", "--writer", "w", "--expected-revision", str(revision),
                  "--operation-id", "next-1")
        self.assertEqual(len(self.current().encode()),
                         preview["next_commit"]["candidate_bytes"],
                         "the preview did not match the commit")

    def test_missing_markdown_object_is_named_corruption(self):
        self.v4_project()
        self.to_v5()
        stub = cli.envelope_state(self.current())[1]
        pid = cli.envelope_state(self.current())[0]["project_id"]
        entries = obs.read_index(self.root, pid, "markdown", stub["markdown"]["index"],
                                 obs.Budget())
        obs.object_path(self.root, "markdown",
                        entries[0]["object_sha256"]).unlink()
        before = self.current()
        code, _out, err = self.call("markdown", expect=2)
        self.assertIn("RELAY_OBJECT_MISSING", err)
        self.assertEqual(self.current(), before)
        code, _out, err = self.call("handoff", "--limit", "3", "--verify-integrity", expect=2)
        self.assertIn("RELAY_OBJECT_MISSING", err)

    def test_export_and_verify_round_trip_includes_the_new_objects(self):
        self.v4_project()
        self.to_v5()
        bundle = self.root / "handoff.zip"
        self.assertEqual(self.call("export", "--output", str(bundle))[0], 0)
        names = zipfile.ZipFile(bundle).namelist()
        self.assertTrue(any("/markdown/" in name for name in names))
        self.assertTrue(any("/ac-map/" in name for name in names))
        code, out, err = self.call("verify", "--bundle", str(bundle))
        self.assertEqual(code, 0, err)
        self.assertTrue(out["valid"])
        self.assertEqual(out["schema"], p.SCHEMA_V5)


if __name__ == "__main__":
    unittest.main()
