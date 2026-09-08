"""Automatic receipt-capacity governance at the CLI boundary."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cli_v2 as cli


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.assertEqual(self.call("init")[0], 0)

    def call(self, *args, patch=None):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main([*args, "--root", str(self.root)])
        return code, json.loads(output.getvalue()) if output.getvalue() else {}, errors.getvalue()

    def test_small_v2_never_implicitly_upgrades(self):
        self.call("resume", "--writer", "a", "--expected-revision", "0", "--operation-id", "r")
        for revision in range(1, 35):
            self.assertEqual(self.call("update", "--writer", "a", "--expected-revision",
                                       str(revision), "--operation-id", f"s{revision}")[0], 0)
        self.assertEqual(self.call("status")[1]["schema"], cli.p.SCHEMA_V2)
        self.assertFalse((self.root / ".relay/receipts").exists())

    def migrate(self, revision):
        preview = self.call("migrate", "--to-v3")[1]
        result = self.call("migrate", "--to-v3", "--apply", "--writer", "a",
                           "--expected-revision", str(revision), "--operation-id", "migrate",
                           "--source-sha256", preview["source_sha256"])
        self.assertEqual(result[0], 0, result[2])

    def make_archived(self):
        self.call("resume", "--writer", "a", "--expected-revision", "0", "--operation-id", "r0")
        for revision in range(1, 36):
            self.assertEqual(self.call("update", "--writer", "a", "--expected-revision",
                                       str(revision), "--operation-id", f"u{revision}")[0], 0)
        self.assertEqual(self.call("save", "--writer", "a", "--expected-revision", "36",
                                   "--operation-id", "save")[0], 0)
        self.migrate(37)

    def test_receipts_are_archived_and_replayable(self):
        self.make_archived()
        status = self.call("status")[1]
        self.assertEqual(status["schema"], cli.p.SCHEMA_V3)
        self.assertEqual(status["current_receipts"], 32)
        code, replay, error = self.call("update", "--writer", "a", "--expected-revision", "1",
                                        "--operation-id", "u1")
        self.assertEqual(code, 0, error)
        self.assertTrue(replay["archived"])

    def test_compact_preview_is_read_only_and_apply_needs_lease(self):
        current = self.root / ".relay/CURRENT.md"
        before = current.read_bytes()
        code, preview, error = self.call("compact")
        self.assertEqual(code, 0, error)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(current.read_bytes(), before)
        code, _, error = self.call("compact", "--apply")
        self.assertEqual(code, 2)
        self.assertIn("expected revision required", error)

    def test_v2_to_v3_migration_is_explicit(self):
        preview = self.call("migrate", "--to-v3")[1]
        self.assertEqual(preview["to_schema"], cli.p.SCHEMA_V3)
        before = (self.root / ".relay/CURRENT.md").read_bytes()
        self.assertEqual(self.call("migrate", "--to-v3", "--apply", "--writer", "a",
                                   "--expected-revision", "0", "--operation-id", "m1",
                                   "--source-sha256", preview["source_sha256"])[0], 0)
        self.assertNotEqual((self.root / ".relay/CURRENT.md").read_bytes(), before)
        self.assertEqual(self.call("status")[1]["schema"], cli.p.SCHEMA_V3)

    def test_handoff_bundle_exports_only_current_and_reachable_receipts(self):
        bundle = self.root / "handoff.zip"
        code, result, error = self.call("export", "--output", str(bundle))
        self.assertEqual(code, 0, error)
        self.assertTrue(result["exported"])
        code, verified, error = self.call("verify", "--bundle", str(bundle))
        self.assertEqual(code, 0, error)
        self.assertTrue(verified["valid"])
        import zipfile
        with zipfile.ZipFile(bundle) as archive:
            self.assertEqual(set(archive.namelist()), {"CURRENT.md", "MANIFEST.json"})
        code, _, error = self.call("export", "--output", str(bundle))
        self.assertEqual(code, 2)
        self.assertIn("overwrite", error)

    def test_corrupt_receipt_chain_is_read_only_degraded(self):
        self.make_archived()
        receipt = next((self.root / ".relay/receipts").iterdir())
        receipt.write_text(receipt.read_text(encoding="utf-8") + "tamper\n", encoding="utf-8")
        status = self.call("status")[1]
        self.assertEqual(status["verification"], "DEGRADED")
        self.assertFalse(status["write_ready"])
        self.assertEqual(self.call("validate")[0], 2)
        self.assertEqual(self.call("resume", "--writer", "b", "--expected-revision", "38",
                                   "--operation-id", "bad")[0], 2)

    def test_v3_manual_mode_and_small_writes_do_not_archive(self):
        self.make_archived()
        count = len(list((self.root / ".relay/receipts").iterdir()))
        for revision in range(38, 42):
            result = self.call("resume", "--writer", "a", "--expected-revision", str(revision),
                               "--operation-id", f"r{revision}", "--no-auto-compact")
            self.assertEqual(result[0], 0, result[2])
        result = self.call("update", "--writer", "a", "--expected-revision", "42",
                           "--operation-id", "automatic-small")
        self.assertEqual(result[0], 0, result[2])
        self.assertFalse(result[1]["compaction"]["applied"])
        self.assertEqual(len(list((self.root / ".relay/receipts").iterdir())), count)

    def test_near_limit_v2_migrates_then_hands_off_losslessly(self):
        # Synthetic fixture only: no activity or business data is copied.
        old, lines, body, meta, state, _ = cli.read(self.root)
        state["operations"] = [{"id": f"fixture-{i}", "hash": "a" * 64, "revision": i}
                               for i in range(300)]
        base = cli.metadata(lines, {"revision": "300"}) + cli.p.render_body(state, body)
        padding = "x" * (65433 - len(base.encode()))
        fixture = base + padding
        self.assertEqual(len(fixture.encode()), 65433)
        current = self.root / ".relay/CURRENT.md"
        current.write_bytes(fixture.encode())
        before = current.read_bytes()
        rejected = self.call("resume", "--writer", "a", "--expected-revision", "300",
                             "--operation-id", "full-v2")
        self.assertEqual(rejected[0], 2)
        self.assertEqual(current.read_bytes(), before)
        self.migrate(300)
        for command, revision, writer in [("resume", 301, "a"), ("update", 302, "a"),
                                           ("save", 303, "a"), ("resume", 304, "b")]:
            result = self.call(command, "--writer", writer, "--expected-revision", str(revision),
                               "--operation-id", f"handoff-{revision}")
            self.assertEqual(result[0], 0, result[2])
        self.assertTrue(current.read_text().endswith(padding))
        self.assertEqual(self.call("validate")[0], 0)

    def test_archive_and_history_failures_leave_current_unchanged(self):
        from unittest.mock import patch
        self.call("resume", "--writer", "a", "--expected-revision", "0", "--operation-id", "r0")
        for revision in range(1, 36):
            self.call("update", "--writer", "a", "--expected-revision", str(revision),
                      "--operation-id", f"f{revision}")
        self.call("save", "--writer", "a", "--expected-revision", "36", "--operation-id", "s")
        current = self.root / ".relay/CURRENT.md"
        before = current.read_bytes()
        preview = self.call("migrate", "--to-v3")[1]
        args = ("migrate", "--to-v3", "--apply", "--writer", "a", "--expected-revision", "37",
                "--operation-id", "transaction", "--source-sha256", preview["source_sha256"])
        for target in ("immutable", "commit"):
            with patch.object(cli.fs, target, side_effect=cli.fs.Error("injected failure")):
                self.assertEqual(self.call(*args)[0], 2)
            self.assertEqual(current.read_bytes(), before)
        self.assertEqual(self.call(*args)[0], 0)
        replay = self.call(*args)
        self.assertEqual(replay[0], 0)
        self.assertTrue(replay[1]["replayed"])

    def test_repeated_growth_archives_in_batches_and_replays(self):
        self.migrate(0)
        self.call("resume", "--writer", "a", "--expected-revision", "1", "--operation-id", "r")
        events = 0
        for revision in range(2, 702):
            result = self.call("update", "--writer", "a", "--expected-revision", str(revision),
                               "--operation-id", f"growth-{revision}")
            self.assertEqual(result[0], 0, result[2])
            events += bool(result[1]["compaction"]["archived_receipts"])
            self.assertLessEqual(result[1]["compaction"]["after_bytes"], cli.p.MAX_BYTES)
        self.assertGreaterEqual(events, 2)
        self.assertLess(events, 10)
        self.assertLess(len(list((self.root / ".relay/receipts").iterdir())), 20)
        replay = self.call("update", "--writer", "a", "--expected-revision", "2",
                           "--operation-id", "growth-2")
        self.assertEqual(replay[0], 0, replay[2])
        self.assertTrue(replay[1]["archived"])
        self.assertEqual(self.call("validate")[0], 0)

    def test_archive_atomic_failures_and_conflicting_retry(self):
        from unittest.mock import patch
        self.make_archived()
        self.call("resume", "--writer", "a", "--expected-revision", "38", "--operation-id", "r38")
        current = self.root / ".relay/CURRENT.md"
        before = current.read_bytes()
        args = ("compact", "--apply", "--writer", "a", "--expected-revision", "39", "--operation-id", "fault")
        real = cli.fs.atomic
        for kind in ("history", "CURRENT.md"):
            def failure(path, document):
                if path.parent.name == kind or path.name == kind:
                    raise cli.fs.Error("injected storage failure")
                return real(path, document)
            with patch.object(cli.fs, "atomic", side_effect=failure):
                self.assertEqual(self.call(*args)[0], 2)
            self.assertEqual(current.read_bytes(), before)
        self.assertEqual(self.call(*args)[0], 0)
        self.assertTrue(self.call(*args)[1]["replayed"])
        self.assertEqual(self.call("update", "--writer", "a", "--expected-revision", "1",
                                   "--operation-id", "u1", "--lease-minutes", "20")[0], 2)

    def test_full_compact_preview_matches_commit_without_writes(self):
        self.migrate(0)
        self.call("resume", "--writer", "a", "--expected-revision", "1", "--operation-id", "r")
        args = ("compact", "--writer", "a", "--expected-revision", "2", "--operation-id", "c")
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        preview = self.call(*args)
        self.assertEqual(preview[0], 0, preview[2])
        self.assertFalse(preview[1]["estimate_only"])
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        applied = self.call(*args, "--apply")
        self.assertEqual(applied[0], 0, applied[2])
        self.assertEqual(preview[1]["candidate_bytes"], applied[1]["compaction"]["after_bytes"])

    def test_bundle_rejects_false_schema(self):
        import zipfile
        self.migrate(0)
        source = self.root / "source.zip"
        self.assertEqual(self.call("export", "--output", str(source))[0], 0)
        with zipfile.ZipFile(source) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(files["MANIFEST.json"])
        manifest["schema"] = cli.p.SCHEMA_V2
        files["MANIFEST.json"] = json.dumps(manifest).encode()
        bad = self.root / "bad.zip"
        with zipfile.ZipFile(bad, "w") as archive:
            for name, value in files.items():
                archive.writestr(name, value)
        self.assertEqual(self.call("verify", "--bundle", str(bad))[0], 2)


if __name__ == "__main__":
    unittest.main()
