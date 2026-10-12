"""v0.5.0 regressions: packaging, null metadata, error context and growth model."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/write_current.py'
sys.path.insert(0, str(ROOT / 'scripts'))
import package_skill  # noqa: E402


def run(root, *args, patch=None, input_path=None):
    command = [sys.executable, str(SCRIPT), *args, '--root', str(root)]
    if input_path is not None:
        command += ['--input', str(input_path)]
    return subprocess.run(command, input=json.dumps(patch) if patch is not None else None,
                          text=True, encoding='utf-8', capture_output=True, timeout=60)


class V05FixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def call(self, *args, patch=None, ok=True, input_path=None):
        result = run(self.root, *args, patch=patch, input_path=input_path)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def leased(self, revision, op):
        self.call('resume', '--writer', 'agent:a', '--expected-revision', str(revision),
                  '--operation-id', op)

    def test_every_test_module_is_packaged(self):
        on_disk = {f"tests/{path.name}" for path in (ROOT / 'tests').glob('test_*.py')}
        self.assertEqual(on_disk - set(package_skill.FILES), set())

    def test_null_metadata_is_reported_as_json_null(self):
        self.call('init')
        status = self.call('status')
        self.assertIsNone(status['writer'])
        self.assertIsNone(status['lease_until'])
        handoff = self.call('handoff')
        self.assertIsNone(handoff['source_identity']['writer'])
        self.assertIsNone(handoff['source_identity']['lease_until'])

    def test_field_errors_name_missing_fields_without_values(self):
        self.call('init')
        self.leased(0, 'op1')
        patch = {'tasks': [{'id': 't1', 'title': 'x', 'acceptance': ['works']}],
                 'evidence': [{'id': 'e1', 'task_id': 't1', 'check': 'secret-check-text',
                               'result': 'pass', 'summary': 'secret-summary-value',
                               'acceptance': ['works']}]}
        result = self.call('update', '--writer', 'agent:a', '--expected-revision', '1',
                           '--operation-id', 'op2', '--input', '-', patch=patch, ok=False)
        error = json.loads(result.stdout or result.stderr)['error']
        self.assertIn('missing [at, ref]', error)
        self.assertIn('unexpected 1 field', error)
        self.assertNotIn('secret', error)
        self.assertNotIn('summary', error)

    def test_symlink_error_names_the_offending_path(self):
        self.call('init')
        self.leased(0, 'op1')
        real = self.root / 'real'
        real.mkdir()
        (real / 'change.json').write_text('{}', encoding='utf-8')
        link = self.root / 'linked'
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are unavailable on this platform')
        result = self.call('update', '--writer', 'agent:a', '--expected-revision', '1',
                           '--operation-id', 'op2', input_path=link / 'change.json', ok=False)
        error = json.loads(result.stdout or result.stderr)['error']
        self.assertIn(str(link), error)
        self.assertIn('--input -', error)

    def test_examples_complete_a_task(self):
        self.call('init')
        self.leased(0, 'op1')
        self.call('update', '--writer', 'agent:a', '--expected-revision', '1',
                  '--operation-id', 'op2', input_path=ROOT / 'examples/change.json')
        self.call('save', '--writer', 'agent:a', '--expected-revision', '2',
                  '--operation-id', 'op3', input_path=ROOT / 'examples/evidence.json')
        tasks = self.call('handoff')['tasks']
        self.assertEqual([task['status'] for task in tasks], ['done'])

    @unittest.skipIf(os.name == 'nt' or os.geteuid() == 0, 'POSIX permissions')
    def test_read_only_relay_names_the_cause(self):
        self.call('init')
        relay = self.root / '.relay'
        relay.chmod(0o500)
        self.addCleanup(relay.chmod, 0o700)
        result = self.call('resume', '--writer', 'agent:a', '--expected-revision', '0',
                           '--operation-id', 'op1', ok=False)
        self.assertIn('read-only', json.loads(result.stderr)['error'])
        self.assertEqual(self.call('status')['revision'], 0)

    def test_growth_model_works_when_the_first_task_is_done(self):
        self.call('init', '--schema', 'v2')
        self.leased(0, 'op1')
        patch = {'tasks': [{'id': 'done-task', 'title': 'x', 'status': 'done',
                            'acceptance': ['works']},
                           {'id': 'open-task', 'title': 'y', 'acceptance': ['later']}],
                 'evidence': [{'id': 'e1', 'task_id': 'done-task', 'check': 'unit',
                               'result': 'pass', 'at': '2026-10-11T00:00:00Z',
                               'ref': 'tests/test_x.py', 'acceptance': ['works']}]}
        self.call('save', '--writer', 'agent:a', '--expected-revision', '1',
                  '--operation-id', 'op2', '--input', '-', patch=patch)
        growth = self.call('capacity')['growth']
        self.assertNotIn('error', growth)
        self.assertEqual(growth['model']['status'], 'modelled')
        self.assertGreater(growth['evidence']['current_bytes'], 0)


class ReferenceAssuranceTests(unittest.TestCase):
    """A claimed pass satisfies the gate but is never reported as verified."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'logs').mkdir()
        (self.root / 'logs/parser.txt').write_text('ok\n', encoding='utf-8')

    def call(self, *args, patch=None):
        result = run(self.root, *args, patch=patch)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def record(self, ref):
        self.call('init')
        self.call('resume', '--writer', 'a', '--expected-revision', '0', '--operation-id', 'r')
        self.call('save', '--writer', 'a', '--expected-revision', '1', '--operation-id', 's',
                  '--input', '-', patch={
                      'tasks': [{'id': 't1', 'title': 'x', 'status': 'done',
                                 'acceptance': ['works']}],
                      'evidence': [{'id': 'e1', 'task_id': 't1', 'check': 'unit',
                                    'result': 'pass', 'at': '2026-10-11T00:00:00Z',
                                    'ref': ref, 'acceptance': ['works']}]})

    def test_missing_reference_is_self_reported(self):
        self.record('logs/missing.txt')
        for view in (self.call('coverage'), self.call('handoff')):
            record = view['verification_record']
            self.assertFalse(record['verified'])
            self.assertEqual(record['reason'], 'evidence_self_reported')
            self.assertEqual(record['assurance'], 'claimed')
            self.assertEqual(record['checks']['references']['self_reported'], ['e1'])
            self.assertTrue(record['checks']['coverage']['verified'])

    def test_resolved_reference_is_verified(self):
        self.record('logs/parser.txt:3')
        coverage = self.call('coverage')
        self.assertTrue(coverage['verification_record']['verified'], coverage['verification_record'])
        self.assertEqual(coverage['verification_record']['assurance'], 'ref_resolved')
        self.assertEqual(coverage['rows'][0]['assurance'], 'ref_resolved')

    def test_every_path_of_a_multi_path_reference_must_exist(self):
        (self.root / 'logs/report.md').write_text('ok\n', encoding='utf-8')
        self.record('logs/parser.txt; logs/report.md#summary')
        self.assertEqual(self.call('coverage')['verification_record']['assurance'], 'ref_resolved')

    def test_one_missing_path_makes_a_multi_path_reference_claimed(self):
        self.record('logs/parser.txt; logs/missing.md')
        self.assertEqual(self.call('coverage')['verification_record']['assurance'], 'claimed')

    def test_reference_outside_the_project_is_claimed(self):
        self.record('../../etc/hosts')
        self.assertEqual(self.call('coverage')['verification_record']['assurance'], 'claimed')

    def test_acknowledged_external_checks_raise_assurance(self):
        self.record('CI run 1234')
        record = self.call('coverage', '--acknowledge-external')['verification_record']
        self.assertEqual(record['assurance'], 'externally_checked')
        self.assertTrue(record['checks']['references']['verified'])


class WindowsPortabilityTests(unittest.TestCase):
    """CRLF documents (a Git autocrlf checkout) and native path separators."""

    def test_custom_region_and_sections_accept_crlf(self):
        import progress as p
        region = "\r\n## Goal\r\n- keep\r\n## State\r\n- now\r\n"
        body = region + p.DATA_START.replace("\n", "\r\n") + "{}" + p.DATA_END.replace("\n", "\r\n")
        self.assertEqual(p.custom_region(body), region)
        self.assertTrue(p.render_region(body, "X").startswith("X<!-- project-continuity:data"))
        sections = p.split_markdown(region)
        self.assertEqual([s["title"] for s in sections], [p.MARKDOWN_PREAMBLE_TITLE, "Goal", "State"])
        self.assertEqual("".join(s["content"] for s in sections), region)

    def test_reachable_object_names_use_forward_slashes(self):
        import objectstore as obs
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            entries = IndexFanoutTests().entries(root, 3)
            plans, sha = obs.plan_index(root, 'p1', 'evidence', entries)
            obs.publish(root, plans)
            with mock.patch.object(obs.Path, 'relative_to', _backslash_relative_to):
                names = obs.index_paths(root, 'p1', 'evidence', sha, obs.Budget())
            self.assertTrue(names)
            self.assertFalse(any('\\' in name for name in names), names)


def _backslash_relative_to(self, *other, **kwargs):
    """Simulate a Windows path whose str() uses backslashes."""
    relative = _ORIGINAL_RELATIVE_TO(self, *other, **kwargs)
    return _WindowsLike(relative)


class _WindowsLike(type(Path())):
    def __str__(self):
        return super().__str__().replace('/', '\\')


_ORIGINAL_RELATIVE_TO = Path.relative_to


class IndexFanoutTests(unittest.TestCase):
    def entries(self, root, count):
        import objectstore as obs
        return [obs.entry_for(root, 'p1', 'evidence',
                              {'id': f'ev-{i:04d}', 'task_id': 't1', 'check': 'c',
                               'result': 'pass', 'at': '2026-10-11T00:00:00Z', 'ref': 'r',
                               'baseline': {}, 'acceptance': [], 'generation': 0})[0]
                for i in range(count)]

    def test_appending_one_record_rewrites_a_bounded_index(self):
        import objectstore as obs
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            existing = self.entries(root, 101)
            before, _ = obs.plan_index(root, 'p1', 'evidence', existing[:100])
            after, _ = obs.plan_index(root, 'p1', 'evidence', existing)
            known = {plan['sha256'] for plan in before}
            new_bytes = sum(len(plan['content']) for plan in after if plan['sha256'] not in known)
            self.assertLess(new_bytes, 8192)

    def test_bounded_index_round_trips_and_old_wide_leaves_stay_readable(self):
        import objectstore as obs
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            entries = self.entries(root, 300)
            plans, sha = obs.plan_index(root, 'p1', 'evidence', entries)
            obs.publish(root, plans)
            self.assertEqual(obs.read_index(root, 'p1', 'evidence', sha, obs.Budget()), entries)
            # A v0.4 writer produced one leaf of up to 512 entries; it stays readable.
            leaf = {'schema': obs.INDEX_SCHEMA, 'project_id': 'p1', 'record_type': 'evidence',
                    'kind': 'leaf', 'count': len(entries), 'entries': entries}
            data = obs.canonical(leaf).encode('utf-8')
            wide = obs.sha256_hex(data)
            obs.publish(root, [{'kind': 'index', 'path': obs.index_path(root, 'evidence', wide),
                                'content': data, 'sha256': wide}])
            self.assertEqual(obs.read_index(root, 'p1', 'evidence', wide, obs.Budget()), entries)


if __name__ == '__main__':
    unittest.main()
