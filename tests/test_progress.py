import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import progress as p


class ProgressTests(unittest.TestCase):
    def test_roundtrip_extensions(self):
        state = p.empty_state("项目")
        state["extensions"] = {"custom": {"hello": [1, True]}}
        body = "## User section\nKeep exactly.\n" + p.render_body(state)
        self.assertEqual(p.parse_body(body), (state, True))
        changed = p.apply(state, {"project": {"goal": "ship"}}, {})
        rendered = p.render_body(changed, body)
        self.assertTrue(rendered.startswith("## User section\nKeep exactly.\n"))
        self.assertEqual(p.parse_body(rendered), (changed, True))

    def task(self):
        return p.apply(p.empty_state(), {"tasks": [{"id": "T1", "title": "Build", "acceptance": ["tests"]}]}, {})

    def test_done_requires_evidence_and_reopen_invalidates(self):
        state = self.task()
        with self.assertRaises(p.Invalid):
            p.apply(state, {"tasks": [{"id": "T1", "status": "done"}]}, {})
        patch = {"tasks": [{"id": "T1", "status": "done"}], "evidence": [{"id": "E1", "task_id": "T1", "check": "unit tests", "result": "pass", "at": "2026-01-01T00:00:00Z", "ref": "tests/report.txt", "acceptance": ["tests"]}]}
        done = p.apply(state, patch, {"kind": "git"})
        self.assertEqual(done["tasks"][0]["status"], "done")
        with self.assertRaises(p.Invalid):
            p.apply(done, {"tasks": [{"id": "T1", "status": "doing"}]}, {})
        reopened = p.apply(done, {"tasks": [{"id": "T1", "status": "doing", "reason": "new requirement"}]}, {})
        self.assertEqual(reopened["tasks"][0]["generation"], 1)
        with self.assertRaises(p.Invalid):
            p.apply(reopened, {"tasks": [{"id": "T1", "status": "done"}]}, {})

    def test_block_and_resolve(self):
        state = self.task()
        with self.assertRaises(p.Invalid):
            p.apply(state, {"tasks": [{"id": "T1", "status": "blocked"}]}, {})
        state = p.apply(state, {"tasks": [{"id": "T1", "status": "blocked"}], "blockers": [{"id": "B1", "task_id": "T1", "status": "open", "description": "needs input"}]}, {})
        state = p.apply(state, {"tasks": [{"id": "T1", "status": "doing"}], "blockers": [{"id": "B1", "status": "resolved", "resolution": "received"}]}, {})
        self.assertFalse(p.summary(state, {})["blockers"])

    def test_duplicate_cycle_and_mismatched_view(self):
        with self.assertRaises(p.Invalid):
            p.loads('{"x":1,"x":2}')
        with self.assertRaises(p.Invalid):
            p.apply(self.task(), {"tasks": [{"id": "T1", "depends_on": ["T1"]}]}, {})
        body = p.render_body(self.task())
        self.assertFalse(p.parse_body(body.replace("## Tasks", "## Fake"))[1])
        with self.assertRaises(p.Invalid):
            p.parse_body(body + body)


if __name__ == "__main__":
    unittest.main()
