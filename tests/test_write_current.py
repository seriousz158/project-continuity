"""Public CLI regression tests, all in isolated temporary project directories."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/write_current.py'
V1 = '''---
schema: project-continuity/v1
project_id: legacy-test
revision: 7
updated_at: 2026-01-01T00:00:00Z
writer: null
lease_until: null
branch: null
base_commit: null
status: blocked
custom_field: preserve-this
---
## 目标
- Keep existing text.
## Project-specific gate
- NOT_EXECUTED is not completion.
'''


def run(root, *args, patch=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args, '--root', str(root)],
                          input=json.dumps(patch) if patch is not None else None,
                          text=True, capture_output=True, timeout=30)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def call(self, *args, patch=None, ok=True):
        result = run(self.root, *args, patch=patch)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def init_resume(self):
        self.call('init')
        return self.call('resume', '--writer', 'agent:a', '--expected-revision', '0', '--operation-id', 'op1')

    def test_status_read_only_explicit_init(self):
        before = list(self.root.rglob('*'))
        self.assertFalse(self.call('status')['exists'])
        self.assertEqual(list(self.root.rglob('*')), before)
        self.call('init')
        current = self.root/'.relay/CURRENT.md'
        before = current.read_bytes()
        self.call('init', ok=False)
        self.assertEqual(current.read_bytes(), before)
        self.call('validate')

    def test_update_save_handoff_and_replay(self):
        self.init_resume()
        args = ('update', '--writer', 'agent:a', '--expected-revision', '1', '--operation-id', 'op2', '--input', '-')
        patch = {'tasks': [{'id': 'T1', 'title': 'Build', 'status': 'doing', 'acceptance': ['unit tests']}]}
        self.assertEqual(self.call(*args, patch=patch)['writer'], 'agent:a')
        self.assertTrue(self.call(*args, patch=patch)['replayed'])
        self.call(*args, patch={'project': {'goal': 'different'}}, ok=False)
        state = self.call('status')
        self.assertEqual(state['counts']['doing'], 1)
        self.call('save', '--writer', 'agent:a', '--expected-revision', '2', '--operation-id', 'op3')
        state = self.call('status')
        self.assertFalse(state['lease_active'])
        self.call('resume', '--writer', 'agent:b', '--expected-revision', '3', '--operation-id', 'op4')
        self.call('save', '--writer', 'agent:a', '--expected-revision', '4', '--operation-id', 'op5', ok=False)
        self.assertEqual(self.call('status')['tasks'][0]['id'], 'T1')

    def test_full_task_lifecycle_does_not_imply_release(self):
        self.init_resume()
        def update(rev, patch):
            return self.call('update', '--writer', 'agent:a', '--expected-revision', str(rev),
                             '--operation-id', 'u' + str(rev), '--input', '-', patch=patch)
        update(1, {'tasks': [{'id': 'T1', 'title': 'Build', 'status': 'doing', 'acceptance': ['tests pass']}]})
        update(2, {'tasks': [{'id': 'T1', 'status': 'blocked'}],
                   'blockers': [{'id': 'B1', 'task_id': 'T1', 'status': 'open', 'description': 'missing fixture'}]})
        update(3, {'tasks': [{'id': 'T1', 'status': 'done'}],
                   'blockers': [{'id': 'B1', 'status': 'resolved', 'resolution': 'fixture provided'}],
                   'evidence': [{'id': 'E1', 'task_id': 'T1', 'check': 'unit tests', 'result': 'pass',
                                 'at': '2026-01-01T00:00:00Z', 'ref': 'test report', 'acceptance': ['tests pass']}],
                   'project': {'status': 'complete', 'next_step': 'Review release separately'}})
        self.call('save', '--writer', 'agent:a', '--expected-revision', '4', '--operation-id', 's5')
        self.call('resume', '--writer', 'agent:b', '--expected-revision', '5', '--operation-id', 'r6')
        state = self.call('status')
        self.assertEqual(state['counts']['done'], 1)
        self.assertFalse(state['blockers'])
        self.assertEqual(state['project']['outcomes'], {})
        self.assertEqual(state['project']['next_step'], 'Review release separately')

    def test_size_and_invalid_document_leave_current_unchanged(self):
        self.init_resume()
        current = self.root / '.relay/CURRENT.md'
        before = current.read_bytes()
        self.call('update', '--writer', 'agent:a', '--expected-revision', '1', '--operation-id', 'large',
                  '--input', '-', patch={'extensions': {'large': 'x' * 65536}}, ok=False)
        self.assertEqual(current.read_bytes(), before)
        current.write_bytes(before.replace(b'revision: 1', b'revision: 1\nrevision: 1'))
        self.call('status', ok=False)

    def test_v1_migration_is_explicit_and_preserves_unknown(self):
        (self.root/'.relay').mkdir()
        current = self.root/'.relay/CURRENT.md'
        current.write_text(V1)
        before = current.read_bytes()
        preview = self.call('migrate')
        self.assertEqual(current.read_bytes(), before)
        self.assertEqual(list((self.root/'.relay').iterdir()), [current])
        self.call('resume', '--writer', 'agent:a', '--expected-revision', '7', '--operation-id', 'x', ok=False)
        self.call('migrate', '--apply', '--writer', 'agent:a', '--expected-revision', '7', '--operation-id', 'm1', '--source-sha256', preview['source_sha256'])
        doc = current.read_text()
        self.assertIn('custom_field: preserve-this', doc)
        self.assertIn('## Project-specific gate\n- NOT_EXECUTED is not completion.', doc)
        self.assertEqual(self.call('status')['schema'], 'project-continuity/v2')
        self.assertFalse(self.call('migrate')['migration_required'])

    def test_concurrent_init_and_resume(self):
        commands = [[sys.executable, str(SCRIPT), 'init', '--root', str(self.root)] for _ in range(2)]
        jobs = [subprocess.Popen(c, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for c in commands]
        [job.communicate(timeout=30) for job in jobs]
        self.assertEqual(sum(job.returncode == 0 for job in jobs), 1)
        commands = [[sys.executable, str(SCRIPT), 'resume', '--root', str(self.root), '--writer', f'a{i}', '--expected-revision', '0', '--operation-id', f'o{i}'] for i in range(2)]
        jobs = [subprocess.Popen(c, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for c in commands]
        [job.communicate(timeout=30) for job in jobs]
        self.assertEqual(sum(job.returncode == 0 for job in jobs), 1)

    def test_sensitive_input_does_not_change_state_or_echo_secret(self):
        self.init_resume()
        current = self.root/'.relay/CURRENT.md'
        before = current.read_bytes()
        secret = 'ghp_' + 'A'*36
        result = self.call('update', '--writer', 'agent:a', '--expected-revision', '1', '--operation-id', 's1', '--input', '-', patch={'project': {'goal': secret}}, ok=False)
        self.assertNotIn(secret, result.stderr)
        self.assertEqual(current.read_bytes(), before)

    def test_git_same_head_dirty_drift(self):
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        self.call('init')
        (self.root/'untracked.txt').write_text('first')
        self.call('resume','--writer','a','--expected-revision','0','--operation-id','r',ok=False)
        self.call('resume','--writer','a','--expected-revision','0','--operation-id','r','--allow-drift')
        before = self.call('status')['git']['fingerprint']
        (self.root/'untracked.txt').write_text('second')
        after = self.call('status')
        self.assertNotEqual(before,after['git']['fingerprint'])
        self.assertTrue(after['drift'])


if __name__ == '__main__':
    unittest.main()
