"""v2 equivalents of observed P1 Red cases plus retry and recovery guarantees."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import cli_v2 as cli
import storage


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.assertEqual(self.call('init')[0],0)
        self.assertEqual(self.call('resume','--writer','a','--expected-revision','0','--operation-id','r')[0],0)
        self.current = self.root/'.relay/CURRENT.md'

    def call(self,*args):
        output=io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code=cli.main([*args,'--root',str(self.root)])
        return code, json.loads(output.getvalue()) if output.getvalue() else {}

    def save(self):
        return self.call('save','--writer','a','--expected-revision','1','--operation-id','s')

    def test_expired_lease_rejected(self):
        lines, body, meta=cli.split(self.current.read_text())
        self.current.write_text(cli.metadata(lines,{'lease_until':'2000-01-01T00:00:00Z'})+body)
        before=self.current.read_bytes()
        self.assertEqual(self.save()[0],2)
        self.assertEqual(self.current.read_bytes(),before)

    def test_history_failure_before_commit(self):
        before=self.current.read_bytes()
        atomic=storage.atomic
        def fail_history(path,document):
            if path.parent.name=='history':
                raise storage.Error('injected history failure')
            return atomic(path,document)
        with mock.patch.object(storage,'atomic',side_effect=fail_history):
            self.assertEqual(self.save()[0],2)
        self.assertEqual(self.current.read_bytes(),before)
        self.assertEqual(self.save()[0],0)

    def test_current_failure_retry_and_lost_response(self):
        before=self.current.read_bytes()
        atomic=storage.atomic
        def fail_current(path,document):
            if path.name=='CURRENT.md':
                raise storage.Error('injected current failure')
            return atomic(path,document)
        with mock.patch.object(storage,'atomic',side_effect=fail_current):
            self.assertEqual(self.save()[0],2)
        self.assertEqual(self.current.read_bytes(),before)
        count=len(list((self.root/'.relay/history').iterdir()))
        self.assertEqual(self.save()[0],0)
        committed=self.current.read_bytes()
        self.assertEqual(len(list((self.root/'.relay/history').iterdir())),count)
        code,result=self.save()
        self.assertEqual(code,0)
        self.assertTrue(result['replayed'])
        self.assertEqual(self.current.read_bytes(),committed)

    def test_recovery_no_overwrite_active_and_monotonic_revision(self):
        args=('recover','--writer','b','--expected-revision','1','--operation-id','rec','--reason','owner requested')
        self.assertEqual(self.call(*args)[0],2)
        self.assertEqual(self.save()[0],0)
        snapshot=next((self.root/'.relay/history').glob('r0-*.md')).name
        code,res=self.call('recover','--writer','b','--expected-revision','2','--operation-id','rec','--reason','restore initial','--snapshot',snapshot)
        self.assertEqual(code,0)
        self.assertEqual(res['revision'],3)
        self.assertEqual(self.call('validate')[0],0)

    def test_explicit_recovery_repairs_only_derived_view(self):
        self.assertEqual(self.save()[0], 0)
        original = self.current.read_text()
        self.current.write_text(original.replace('## Tasks', '## Tampered display'))
        self.assertEqual(self.call('validate')[0], 2)
        code, result = self.call('recover', '--writer', 'b', '--expected-revision', '2',
                                 '--operation-id', 'fix-view', '--reason', 'Regenerate reviewed display')
        self.assertEqual(code, 0)
        self.assertNotIn('Tampered display', self.current.read_text())
        self.assertNotIn(str(self.root), result['history'])
        self.assertEqual(self.call('validate')[0], 0)


if __name__=='__main__':
    unittest.main()
