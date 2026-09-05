"""Security and recovery regression cases using synthetic values only."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cli_v2 as cli
import progress


class SecurityRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.assertEqual(self.call("init")[0], 0)
        self.current = self.root / ".relay/CURRENT.md"

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*args, "--root", str(self.root)])
        return code, out.getvalue(), err.getvalue()

    def state_document(self, edit):
        lines, body, meta = cli.split(self.current.read_text())
        state, _ = progress.parse_body(body)
        edit(state)
        return cli.metadata(lines, {}) + progress.render_body(state, body)

    def test_benign_task_identifier_is_not_a_credential(self):
        cli.scan("task-004-auto-pressure")
        doc = self.state_document(lambda s: s["project"].update({"goal": "task-004-auto-pressure"}))
        self.current.write_text(doc)
        self.assertEqual(self.call("validate")[0], 0)

    def test_separated_secret_prefix_and_escaped_sk_are_rejected(self):
        fake = "sk-" + "A" * 24
        with self.assertRaises(progress.Invalid):
            cli.scan("credential " + fake)
        doc = self.state_document(lambda s: s["extensions"].update({"value": fake}))
        self.current.write_text(doc.replace(fake, r"\u0073k-" + "A" * 24))
        code, out, err = self.call("status")
        self.assertEqual(code, 2)
        self.assertNotIn(fake, out + err)

    def test_decoded_state_sensitive_values_are_rejected_before_status_output(self):
        fake = "ghp_" + "A" * 36
        doc = self.state_document(lambda s: s["project"]["outcomes"].update({"external": fake}))
        self.current.write_text(doc.replace(fake, r"\u0067hp_" + "A" * 36))
        before = self.current.read_bytes()
        code, out, err = self.call("status")
        self.assertEqual(code, 2)
        self.assertNotIn(fake, out + err)
        self.assertEqual(self.current.read_bytes(), before)

    def test_decoded_extensions_are_also_scanned(self):
        fake = "ghp_" + "B" * 36
        doc = self.state_document(lambda s: s["extensions"].update({"nested": {"value": fake}}))
        self.current.write_text(doc.replace(fake, r"\u0067hp_" + "B" * 36))
        code, out, err = self.call("validate")
        self.assertEqual(code, 2)
        self.assertNotIn(fake, out + err)

    def test_decoded_git_baseline_is_scanned(self):
        fake = "ghp_" + "C" * 36
        lines, body, _ = cli.split(self.current.read_text())
        raw = json.dumps({"kind": "none", "extra": fake}).replace(fake, r"\u0067hp_" + "C" * 36)
        self.current.write_text(cli.metadata(lines, {"git_baseline": raw}) + body)
        code, out, err = self.call("status")
        self.assertEqual(code, 2)
        self.assertNotIn(fake, out + err)

    def test_snapshot_recovery_preserves_current_extension_overlay(self):
        self.assertEqual(self.call("resume", "--writer", "a", "--expected-revision", "0", "--operation-id", "r")[0], 0)
        self.current.write_text(self.state_document(lambda s: s["extensions"].update({"keep": {"nested": "current"}})))
        self.assertEqual(self.call("save", "--writer", "a", "--expected-revision", "1", "--operation-id", "s")[0], 0)
        snapshot = next((self.root / ".relay/history").glob("r0-*.md")).name
        self.assertEqual(self.call("recover", "--writer", "b", "--expected-revision", "2", "--operation-id", "rec", "--reason", "restore", "--snapshot", snapshot)[0], 0)
        state = cli.read(self.root)[4]
        self.assertEqual(state["extensions"]["keep"], {"nested": "current"})
        self.assertEqual(len(state["extensions"]["recovery_log"]), 1)
        self.assertEqual(len(state["operations"]), 3)

    def test_malformed_reserved_recovery_log_fails_closed(self):
        self.current.write_text(self.state_document(lambda s: s["extensions"].update({"recovery_log": "not a list"})))
        before = self.current.read_bytes()
        code, out, err = self.call("recover", "--writer", "b", "--expected-revision", "0", "--operation-id", "rec", "--reason", "recover")
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.current.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
