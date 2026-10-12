"""gc: explicit, previewed collection of old history and unreachable objects."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cli_v2 as cli

STAMP = "2026-10-11T00:00:00Z"


class _FakeStdin:
    def __init__(self, payload: bytes):
        self.buffer = io.BytesIO(payload)


def tree(root):
    """Every path under .relay with its size and modification time."""
    relay = root / ".relay"
    return {str(path.relative_to(relay)): (path.lstat().st_size, path.lstat().st_mtime_ns)
            for path in relay.rglob("*")}


class GcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.call("init")
        self.call("resume", "--writer", "a", "--expected-revision", "0", "--operation-id", "r0")
        self.call("update", "--writer", "a", "--expected-revision", "1", "--operation-id", "u1",
                  patch={"tasks": [{"id": "t1", "title": "x", "acceptance": [f"c{i}" for i in range(6)]}]})
        # Each evidence append rewrites the index, so older index nodes end up
        # reachable only from older history snapshots.
        for i in range(6):
            self.call("update", "--writer", "a", "--expected-revision", str(2 + i),
                      "--operation-id", f"e{i}",
                      patch={"evidence": [{"id": f"e{i}", "task_id": "t1", "check": "unit",
                                           "result": "pass", "at": STAMP, "ref": "r",
                                           "acceptance": [f"c{i}"]}]})
        self.revision = 8

    def call(self, *args, patch=None, expect=0):
        argv = list(args) + (["--input", "-"] if patch is not None else [])
        out, err = io.StringIO(), io.StringIO()
        stdin = (mock.patch.object(sys, "stdin", _FakeStdin(json.dumps(patch).encode("utf-8")))
                 if patch is not None else contextlib.nullcontext())
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), stdin:
            code = cli.main(argv + ["--root", str(self.root)])
        if expect is not None:
            self.assertEqual(code, expect, err.getvalue() or out.getvalue())
        body = out.getvalue().strip()
        return json.loads(body) if body else json.loads(err.getvalue() or "{}")

    def history(self):
        return sorted(path.name for path in (self.root / ".relay/history").iterdir())

    def test_preview_makes_no_writes_and_names_what_it_would_collect(self):
        before = tree(self.root)
        report = self.call("gc", "--keep-history", "2")
        self.assertEqual(tree(self.root), before)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["history"]["total"], self.revision)
        self.assertEqual(report["history"]["retained"], 2)
        self.assertEqual(report["history"]["reclaimable"], self.revision - 2)
        self.assertGreater(report["objects"]["reclaimable"], 0)
        self.assertEqual(report["receipt_segments"]["policy"], "never collected")
        # The default keeps 50 snapshots, so a young project has nothing to drop.
        self.assertEqual(self.call("gc")["history"]["reclaimable"], 0)

    def test_apply_requires_the_live_lease(self):
        before = tree(self.root)
        error = self.call("gc", "--apply", expect=2)["error"]
        self.assertIn("--writer and --expected-revision", error)
        error = self.call("gc", "--apply", "--writer", "b", "--expected-revision",
                          str(self.revision), expect=2)["error"]
        self.assertIn("lease", error)
        error = self.call("gc", "--apply", "--writer", "a", "--expected-revision", "1",
                          expect=2)["error"]
        self.assertIn("revision conflict", error)
        before.pop("CURRENT.md.lock", None)
        after = tree(self.root)
        after.pop("CURRENT.md.lock", None)
        self.assertEqual(after, before)

    def test_apply_keeps_integrity_recovery_and_foreign_files(self):
        relay = self.root / ".relay"
        receipts = sorted((relay / "receipts").rglob("*")) if (relay / "receipts").exists() else []
        foreign = relay / "objects/NOTES.txt"
        foreign.write_text("not an object\n", encoding="utf-8", newline="\n")
        orphan_sha = "ab" + "0" * 62
        orphan = relay / "objects/evidence/ab" / f"{orphan_sha}.json"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_text("{}", encoding="utf-8", newline="\n")
        preview = self.call("gc", "--keep-history", "2")
        self.assertIn(f"objects/evidence/ab/{orphan_sha}.json", preview["objects"]["sample"])
        self.assertIn("objects/NOTES.txt", preview["objects"]["unrecognized"])

        result = self.call("gc", "--apply", "--keep-history", "2", "--writer", "a",
                           "--expected-revision", str(self.revision))
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["deleted"]["history"], preview["history"]["reclaimable"])
        self.assertEqual(result["deleted"]["objects"], preview["objects"]["reclaimable"])
        self.assertEqual(len(self.history()), 2)
        self.assertFalse(orphan.exists())
        self.assertTrue(foreign.exists())
        self.assertEqual(sorted((relay / "receipts").rglob("*")) if receipts else [], receipts)
        self.assertEqual(self.call("handoff", "--verify-integrity")["checks"]["integrity"]["state"],
                         "verified")
        self.assertEqual(self.call("gc", "--keep-history", "2")["objects"]["reclaimable"], 0)

        # A retained snapshot still recovers: every object it reaches was kept.
        oldest = self.history()[0]
        self.call("save", "--writer", "a", "--expected-revision", str(self.revision),
                  "--operation-id", "s1", patch={})
        recovered = self.call("recover", "--writer", "a", "--expected-revision",
                              str(self.revision + 1), "--operation-id", "rec1",
                              "--reason", "gc check", "--snapshot", oldest)
        self.assertEqual(recovered["revision"], self.revision + 2)
        self.assertEqual(self.call("handoff", "--verify-integrity")["checks"]["integrity"]["state"],
                         "verified")

    def test_unparseable_hand_named_snapshot_blocks_object_collection(self):
        history = self.root / ".relay/history"
        (history / "20260101T000000Z-r3-manual.md").write_text("not a relay document\n",
                                                                 encoding="utf-8")
        report = self.call("gc", "--keep-history", "2")
        self.assertEqual(report["history"]["unrecognized"], 1)
        self.assertEqual(report["history"]["unrecognized_unresolved"], 1)
        self.assertEqual(report["objects"]["reclaimable"], 0)
        self.assertIn("cannot be parsed", report["objects"]["blocked"])
        self.call("gc", "--apply", "--keep-history", "2", "--writer", "a",
                  "--expected-revision", str(self.revision))
        self.assertTrue((history / "20260101T000000Z-r3-manual.md").exists())
        self.assertEqual(self.call("handoff", "--verify-integrity")["checks"]["integrity"]["state"],
                         "verified")

    def test_missing_reachable_object_refuses_to_collect(self):
        # Every evidence object is still reachable from CURRENT.
        target = sorted((self.root / ".relay/objects/evidence").rglob("*.json"))[0]
        target.unlink()
        before = tree(self.root)
        error = self.call("gc", "--keep-history", "1", expect=2)["error"]
        self.assertIn("1 reachable object file(s) are missing", error)
        self.assertEqual(tree(self.root), before)


if __name__ == "__main__":
    unittest.main()
