"""Atomic Relay pruning with v5 external objects."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
WRITE = SCRIPTS / "write_current.py"
sys.path.insert(0, str(SCRIPTS))

import cli_v2 as cli
import progress as p
import v4 as relay_v4


class PruneCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.call("init", "--name", "prune fixture")
        self.call("resume", "--writer", "w", "--expected-revision", "0",
                  "--operation-id", "r1")
        patch = {
            "tasks": [
                {"id": "old-a", "title": "Old A", "status": "todo", "acceptance": ["a"]},
                {"id": "old-b", "title": "Old B", "status": "todo", "acceptance": ["b"]},
                {"id": "current", "title": "Current", "status": "doing", "acceptance": ["c"]},
            ],
            "blockers": [
                {"id": "block-a", "task_id": "old-a", "status": "open", "description": "old blocker a"},
                {"id": "block-b", "task_id": "old-b", "status": "resolved", "description": "old blocker b",
                 "resolution": "closed"},
            ],
            "evidence": [
                {"id": "ev-a", "task_id": "old-a", "check": "old a", "result": "pass",
                 "at": "2026-01-01T00:00:00Z", "ref": "runs/ev-a", "acceptance": ["a"]},
                {"id": "ev-b", "task_id": "old-b", "check": "old b", "result": "fail",
                 "at": "2026-01-01T00:00:00Z", "ref": "runs/ev-b", "acceptance": ["b"]},
                {"id": "ev-current", "task_id": "current", "check": "current", "result": "pass",
                 "at": "2026-01-01T00:00:00Z", "ref": "runs/current", "acceptance": ["c"]},
            ],
            "decisions": [{"id": "decision-old", "task_ids": ["old-a"],
                           "conclusion": "obsolete", "reason": "fixture"}],
        }
        self.call("save", "--writer", "w", "--expected-revision", "1",
                  "--operation-id", "s1", patch=patch)
        self.to_v4()
        self.to_v5()

    def call(self, *args, patch=None, expect=0):
        argv = [sys.executable, "-B", str(WRITE), *args, "--root", str(self.root)]
        if patch is not None:
            argv += ["--input", "-"]
        result = subprocess.run(argv, input=json.dumps(patch) if patch is not None else None,
                                text=True, encoding="utf-8", capture_output=True,
                                cwd=str(SKILL), timeout=120)
        if expect is not None:
            self.assertEqual(result.returncode, expect, result.stderr or result.stdout)
        return json.loads(result.stdout) if result.stdout.strip() else {}, result.stderr

    def to_v4(self):
        preview, _ = self.call("migrate", "--to-v4")
        rev = self.call("status")[0]["revision"]
        self.call("migrate", "--to-v4", "--apply", "--writer", "w",
                  "--expected-revision", str(rev), "--operation-id", "m4",
                  "--source-sha256", preview["source_sha256"])

    def to_v5(self):
        preview, _ = self.call("migrate", "--to-v5")
        rev = self.call("status")[0]["revision"]
        self.call("migrate", "--to-v5", "--apply", "--writer", "w",
                  "--expected-revision", str(rev), "--operation-id", "m5",
                  "--source-sha256", preview["source_sha256"])

    def manifest(self):
        current = (self.root / ".relay" / "CURRENT.md").read_text(encoding="utf-8")
        state = relay_v4.resolve(current, self.root)
        old = {"old-a", "old-b"}
        return {
            "prune": {
                "task_ids": sorted(old),
                "blocker_ids": sorted(r["id"] for r in state["blockers"] if r["task_id"] in old),
                "evidence_ids": sorted(r["id"] for r in state["evidence"] if r["task_id"] in old),
                "decision_ids": sorted(r["id"] for r in state["decisions"] if set(r["task_ids"]) & old),
                "reason": "remove superseded fixture history",
                "target_revision": self.call("status")[0]["revision"],
            },
            "project": {"next_step": "continue current task"},
        }

    def test_incomplete_cascade_is_rejected(self):
        current = (self.root / ".relay" / "CURRENT.md").read_text(encoding="utf-8")
        state = relay_v4.resolve(current, self.root)
        spec = self.manifest()["prune"]
        spec["blocker_ids"] = []
        with self.assertRaises(p.Invalid):
            p.prune(state, spec)

    def test_capacity_preview_and_atomic_prune(self):
        manifest = self.manifest()
        before = (self.root / ".relay" / "CURRENT.md").read_bytes()
        preview, _ = self.call("capacity", patch=manifest)
        planned = preview["patch_preview"]
        self.assertEqual(planned["commands"], ["resume", "update", "save"])
        self.assertTrue(planned["would_fit"])
        self.assertEqual((self.root / ".relay" / "CURRENT.md").read_bytes(), before)

        rev = self.call("status")[0]["revision"]
        self.call("resume", "--writer", "w", "--expected-revision", str(rev),
                  "--operation-id", "r-prune")
        rev += 1
        manifest["prune"]["target_revision"] = rev
        before_dry = (self.root / ".relay" / "CURRENT.md").read_bytes()
        dry, _ = self.call("prune", "--writer", "w", "--expected-revision", str(rev),
                           "--operation-id", "prune-preview", patch=manifest)
        self.assertTrue(dry["dry_run"])
        self.assertTrue(dry["would_fit"])
        self.assertEqual((self.root / ".relay" / "CURRENT.md").read_bytes(), before_dry)

        committed, _ = self.call("prune", "--apply", "--writer", "w",
                                 "--expected-revision", str(rev),
                                 "--operation-id", "prune-apply", patch=manifest)
        self.assertEqual(committed["revision"], rev + 1)
        self.call("save", "--writer", "w", "--expected-revision", str(rev + 1),
                  "--operation-id", "s-prune")
        state = relay_v4.resolve((self.root / ".relay" / "CURRENT.md").read_text(), self.root)
        self.assertEqual([r["id"] for r in state["tasks"]], ["current"])
        self.assertEqual([r["id"] for r in state["evidence"]], ["ev-current"])
        self.assertFalse(state["blockers"])
        self.assertFalse(state["decisions"])
        self.assertEqual(self.call("validate")[0]["document_valid"], True)
        self.assertFalse(self.call("status")[0]["lease_active"])
        replay, _ = self.call("prune", "--apply", "--writer", "w",
                              "--expected-revision", str(rev),
                              "--operation-id", "prune-apply", patch=manifest)
        self.assertTrue(replay["replayed"])


if __name__ == "__main__":
    unittest.main()
