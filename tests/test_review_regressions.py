"""Targeted audit regressions with synthetic local state only."""
from test_v6_archive import V6Case
import cli_v2 as cli
import objectstore as obs


class CoveragePaginationTests(V6Case):
    def setUp(self):
        super().setUp()
        (self.root / 'report.md').write_text('synthetic test evidence', encoding='utf-8')
        self.call('init')
        self.call('save', '--acquire', '--writer', 'a', '--expected-revision', '0',
                  '--operation-id', 'seed', patch={
                      'tasks': [{'id': 't', 'title': 'test', 'acceptance': ['A', 'B']}],
                      'evidence': [self.evidence('e1', ['A'])]})

    @staticmethod
    def evidence(key, acceptance):
        return {'id': key, 'task_id': 't', 'check': 'unit', 'result': 'pass',
                'at': '2026-10-24T12:00:00Z', 'ref': 'report.md', 'acceptance': acceptance}

    def test_truncated_coverage_is_not_verified(self):
        result = self.call('coverage', '--limit', '1')
        self.assertTrue(result['pagination']['has_more'])
        self.assertFalse(result['verification_record']['verified'])
        self.assertFalse(result['verification_record']['complete'])
        self.assertEqual(result['verification_record']['reason'], 'pagination_incomplete')
        full = self.call('coverage', '--limit', '20')
        self.assertTrue(full['verification_record']['complete'])
        self.assertFalse(full['verification_record']['verified'])
        self.assertEqual(full['checks']['coverage']['uncovered'], 1)

    def test_complete_pass_and_offset_page_are_distinguished(self):
        self.call('save', '--acquire', '--writer', 'a', '--expected-revision', '1',
                  '--operation-id', 'e2', patch={'evidence': [self.evidence('e2', ['B'])]})
        self.assertTrue(self.call('coverage')['verification_record']['verified'])
        first = self.call('coverage', '--task', 't', '--limit', '1')
        self.assertFalse(first['verification_record']['verified'])
        tail = self.call('coverage', '--task', 't', '--acceptance-cursor', 't:1', '--limit', '20')
        self.assertFalse(tail['verification_record']['verified'])
        self.assertFalse(tail['verification_record']['complete'])

    def test_nested_evidence_page_does_not_claim_complete(self):
        self.call('save', '--acquire', '--writer', 'a', '--expected-revision', '1',
                  '--operation-id', 'many', patch={
                      'evidence': [self.evidence('e2', ['A', 'B']), self.evidence('e3', ['A', 'B'])]})
        page = self.call('coverage', '--limit', '2')
        self.assertTrue(any(row['truncated'] for row in page['rows']))
        self.assertFalse(page['verification_record']['complete'])
        self.assertFalse(page['verification_record']['verified'])


class SnapshotRecoveryTests(V6Case):
    def setUp(self):
        super().setUp()
        self.call('init')
        self.call('save', '--acquire', '--writer', 'a', '--expected-revision', '0',
                  '--operation-id', 'task', patch={'tasks': [{'id': 't', 'title': 'work', 'acceptance': ['A']}]})
        self.call('save', '--acquire', '--writer', 'a', '--expected-revision', '1',
                  '--operation-id', 'evidence', patch={'evidence': [CoveragePaginationTests.evidence('e1', ['A'])]})
        self.snapshot = next((self.root / '.relay/history').glob('r1-*.md')).name
        self.object = next((self.root / '.relay/objects/evidence').rglob('*.json'))

    def recover(self, revision=2, expect=0, snapshot=True):
        return self.call('recover', '--writer', 'b', '--expected-revision', str(revision),
                         '--operation-id', 'recover', '--reason', 'synthetic recovery drill',
                         *(['--snapshot', self.snapshot] if snapshot else []), expect=expect)

    def test_snapshot_recovers_missing_current_object(self):
        self.object.rename(self.root / '.relay/quarantined-evidence.json')
        result = self.recover()
        self.assertEqual(result['revision'], 3)
        state = cli.resolve((self.root / '.relay/CURRENT.md').read_text(), self.root)
        self.assertEqual(state['evidence'], [])
        self.assertEqual(len(state['operations']), 3)
        self.assertEqual(self.call('validate', '--verify-integrity')['checks']['integrity']['state'], 'verified')
        self.assertTrue(self.recover()['replayed'])

    def test_corruption_does_not_bypass_cas_or_lease(self):
        self.leased(2, 'lease')
        self.object.write_bytes(self.object.read_bytes() + b' ')
        before = (self.root / '.relay/CURRENT.md').read_bytes()
        error = self.recover(revision=2, expect=2)
        self.assertIn('revision conflict', error['error'])
        error = self.recover(revision=3, expect=2)
        self.assertIn('active lease', error['error'])
        self.assertEqual((self.root / '.relay/CURRENT.md').read_bytes(), before)

    def test_snapshot_dependencies_must_resolve_and_no_snapshot_still_fails(self):
        self.object.write_bytes(self.object.read_bytes() + b' ')
        before = (self.root / '.relay/CURRENT.md').read_bytes()
        self.recover(snapshot=False, expect=2)
        _text, _lines, _body, meta, stub, _matches = cli.read_envelope(self.root)
        entry = obs.read_index(self.root, meta['project_id'], 'ac-map', stub['ac_map']['index'], obs.Budget())[0]
        shared = obs.object_path(self.root, 'ac-map', entry['object_sha256'])
        shared.write_bytes(shared.read_bytes() + b' ')
        self.recover(expect=2)
        self.assertEqual((self.root / '.relay/CURRENT.md').read_bytes(), before)


class ProtocolReferenceAssuranceTests(V6Case):
    def setUp(self):
        super().setUp()
        self.call('init')
        self.project_id = cli.read_envelope(self.root)[3]['project_id']

    def object_file(self, payload, record_type='evidence', project_id=None):
        data = obs.envelope_text(project_id or self.project_id, record_type, payload).encode()
        sha = obs.sha256_hex(data)
        path = obs.object_path(self.root, record_type, sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path, sha

    def assert_claimed(self, path, sha):
        relative = '/'.join(path.relative_to(self.root / '.relay').parts)
        self.assertEqual(cli._grade_reference(self.root, relative), 'claimed')
        self.assertEqual(cli._grade_reference(self.root, sha), 'claimed')

    def test_pseudo_protocol_and_cross_project_objects_are_not_bound(self):
        data = b'{"id":"fake","not":"a protocol envelope"}'
        sha = obs.sha256_hex(data)
        path = obs.object_path(self.root, 'evidence', sha)
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        self.assert_claimed(path, sha)
        path, sha = self.object_file({'id': 'other'}, project_id='another-project')
        self.assert_claimed(path, sha)

    def test_ancestor_symlink_is_not_followed_in_either_branch(self):
        import tempfile
        data = obs.envelope_text(self.project_id, 'evidence', {'id': 'test'}).encode()
        sha = obs.sha256_hex(data)
        path = obs.object_path(self.root, 'evidence', sha)
        path.parent.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as raw:
            target = cli.fs.root_path(raw)
            (target / path.name).write_bytes(data)
            try:
                path.parent.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest('symlinks unavailable')
            self.assert_claimed(path, sha)

    def test_valid_objects_indexes_and_plain_paths_keep_their_grades(self):
        path, sha = self.object_file({'id': 'valid'})
        relative = '/'.join(path.relative_to(self.root / '.relay').parts)
        self.assertEqual(cli._grade_reference(self.root, relative), 'ref_bound')
        self.assertEqual(cli._grade_reference(self.root, sha), 'ref_bound')
        self.assertEqual(cli._grade_reference(self.root, path.as_posix()), 'ref_bound')
        _doc, _lines, _body, meta, stub, _matches = cli.read_envelope(self.root)
        index = obs.index_path(self.root, 'ac-map', stub['ac_map']['index'])
        self.assertEqual(cli._grade_reference(self.root, '/'.join(index.relative_to(self.root / '.relay').parts)), 'ref_bound')
        (self.root / 'report.md').write_text('plain evidence', encoding='utf-8')
        self.assertEqual(cli._grade_reference(self.root, 'report.md'), 'ref_resolved')
        path.write_bytes(path.read_bytes() + b' ')
        self.assert_claimed(path, sha)

    def test_foreign_namespace_and_prefix_are_not_reinterpreted(self):
        path, sha = self.object_file({'id': 'valid'})
        self.assertEqual(cli._grade_reference(self.root, '../outside/.relay/' + '/'.join(path.relative_to(self.root / '.relay').parts)), 'claimed')
        data = obs.envelope_text(self.project_id, 'evidence', {'id': 'wrong-type'}).encode()
        sha = obs.sha256_hex(data)
        wrong = obs.object_path(self.root, 'correction', sha)
        wrong.parent.mkdir(parents=True, exist_ok=True); wrong.write_bytes(data)
        self.assert_claimed(wrong, sha)


class StatusVerificationTests(V6Case):
    def test_matching_baseline_pass_is_not_a_failure(self):
        self.call('init')
        report = self.call('status')
        self.assertEqual(report['checks']['baseline']['state'], 'pass')
        self.assertTrue(report['checks']['baseline']['verified'])
        self.assertTrue(report['verification_record']['verified'])
        self.assertIsNone(report['verification_record']['reason'])
        self.assertTrue(report['write_ready'])

    def test_stale_and_unchecked_baseline_remain_unverified(self):
        self.call('init')
        (self.root / 'new.txt').write_text('changed', encoding='utf-8')
        report = self.call('status')
        self.assertFalse(report['verification_record']['verified'])
        self.assertFalse(report['write_ready'])
        from unittest import mock
        with mock.patch.object(cli.git_state, 'capture', return_value={'kind': 'error'}):
            report = self.call('status')
        self.assertFalse(report['verification_record']['verified'])
        self.assertFalse(report['write_ready'])
