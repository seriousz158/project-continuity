"""Explicit decision archival uses v6 storage without changing task identity."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from test_v6_archive import V6Case, big_patch
import cli_v2 as cli
import migrate_compare
import progress as p
import v4

STAMP = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


def decision(key, size=40, task='live'):
    return {'id': key, 'task_ids': [task], 'conclusion': 'c' * size, 'reason': 'evidence/report.md'}


class DecisionArchiveTests(V6Case):
    def setUp(self):
        super().setUp()
        self.call('init', frozen=STAMP)
        self.call('save', '--acquire', '--writer', 'a', '--expected-revision', '0',
                  '--operation-id', 'seed', frozen=STAMP, patch={
                      'project': {'current_task': 'live'},
                      'tasks': [{'id': 'live', 'title': 'Long running work', 'status': 'doing',
                                 'acceptance': ['A', 'B', 'C']}],
                      'decisions': [decision('d1'), decision('d2')]})

    def write(self, patch, revision=1, op='archive', command='save', acquire=True, expect=0):
        return self.call(command, *(['--acquire'] if acquire else []), '--writer', 'a',
                         '--expected-revision', str(revision), '--operation-id', op,
                         patch=patch, expect=expect, frozen=STAMP)

    def state(self):
        text = (self.root / '.relay/CURRENT.md').read_text()
        return text, cli.resolve(text, self.root)

    def test_archive_and_pointer_are_atomic_lossless_and_discoverable(self):
        before, state = self.state()
        original = copy.deepcopy(state['decisions'][0])
        result = self.write({'archive_decisions': ['d1'], 'decisions': [decision('pointer')]})
        after, hot = self.state()
        self.assertEqual(hot['tasks'], state['tasks'])
        self.assertEqual(hot['project']['current_task'], 'live')
        self.assertEqual([r['id'] for r in hot['decisions']], ['d2', 'pointer'])
        self.assertNotIn('archive_decisions', hot)
        self.assertEqual(result['archive']['explicit'], 1)
        record = self.call('archive', '--id', 'd1')['records'][0]
        self.assertEqual(record['record'], original)
        self.assertIn('explicit', record['reason'])
        handoff = self.call('handoff', '--task', 'live')
        self.assertEqual(handoff['archived_decisions']['count'], 1)
        self.assertIn('archive --task live', handoff['archived_decisions']['read'])
        self.assertEqual(self.call('archive', '--task', 'live')['total'], 1)
        self.assertIn('archive --task live', self.call('status', '--brief')['progress_summary']['archived_decisions_hint'])
        bundle = self.root / 'handoff.zip'
        self.call('export', '--output', str(bundle))
        self.assertTrue(self.call('verify', '--bundle', str(bundle))['valid'])

    def test_archive_only_compares_equivalent(self):
        before, _ = self.state()
        self.write({'archive_decisions': ['d1']})
        after, _ = self.state()
        same, report = migrate_compare.compare(before, after, self.root)
        self.assertTrue(same, report)

    def test_preview_matches_save_and_automatic_operation_replay(self):
        patch = {'archive_decisions': ['d1'], 'decisions': [decision('pointer')]}
        preview = self.call('capacity', '--acquire', '--writer', 'a', '--expected-revision', '1',
                            patch=patch, frozen=STAMP)['patch_preview']
        result = self.call('save', '--acquire', '--writer', 'a', '--expected-revision', '1',
                           patch=patch, frozen=STAMP)
        self.assertEqual(preview['final_candidate_bytes'], result['capacity']['used_bytes'])
        self.assertEqual(self.call('save', '--acquire', '--writer', 'a', '--expected-revision', '1',
                                   patch=patch, frozen=STAMP)['replayed'], True)
        self.assertEqual(self.call('archive')['total'], 1)
        rejected = self.write({'archive_decisions': ['d2']}, op=result['operation_id'], expect=2)
        self.assertIn('operation ID reused', rejected['error'])

    def test_update_and_ordinary_save_share_operation(self):
        self.leased(1, 'resume', frozen=STAMP)
        self.write({'archive_decisions': ['d1']}, revision=2, command='update', acquire=False)
        self.write({'archive_decisions': ['d2']}, revision=3, op='save', acquire=False)
        self.assertEqual(self.call('archive')['total'], 2)

    def test_conflicts_missing_ids_and_invalid_lists_are_atomic(self):
        before, _ = self.state()
        for patch in ({'archive_decisions': ['d1'], 'decisions': [decision('d1')]},
                      {'archive_decisions': ['missing']}, {'archive_decisions': ['d1', 'd1']},
                      {'archive_decisions': 'd1'}, {'archive_decisions': [{}]}):
            self.write(patch, expect=2)
            self.assertEqual(self.state()[0], before)
        self.write({'archive_decisions': ['d1']})
        error = self.write({'decisions': [decision('d1')]}, revision=2, op='reuse', expect=2)
        self.assertIn('archived decision ID', error['error'])

    def test_hot_correction_blocks_including_same_patch(self):
        correction = {'id': 'c1', 'kind': 'revocation', 'target_type': 'decision',
                      'target_id': 'd1', 'replacement_id': None, 'reason': 'withdrawn', 'at': '2026-10-24T12:00:00Z'}
        before, _ = self.state()
        error = self.write({'corrections': [correction], 'archive_decisions': ['d1']}, expect=2)
        self.assertIn('c1', error['error'])
        self.assertIn('decision', error['error'])
        self.assertEqual(self.state()[0], before)
        self.write({'corrections': [correction]}, op='correction')
        error = self.write({'archive_decisions': ['d1']}, revision=2, expect=2)
        self.assertIn('c1', error['error'])
        report = self.call('capacity')['decision_archival']
        self.assertEqual(report['blocked'][0]['id'], 'd1')
        self.assertIn('c1', str(report['blocked'][0]))

    def test_legacy_schemas_explicitly_refused(self):
        import tempfile
        for schema in (p.SCHEMA_V2, p.SCHEMA_V3, p.SCHEMA_V4, p.SCHEMA_V5):
            with tempfile.TemporaryDirectory() as raw:
                saved = self.root
                self.root = Path(raw).resolve()
                try:
                    self.call('init', '--schema', 'v2')
                    if schema != p.SCHEMA_V2:
                        flags = {p.SCHEMA_V3: '--to-v3', p.SCHEMA_V4: '--to-v4',
                                 p.SCHEMA_V5: '--to-latest'}
                        if schema == p.SCHEMA_V5:
                            for flag in ('--to-v4', '--to-v5'):
                                dry = self.call('migrate', flag)
                                self.call('migrate', flag, '--apply', '--source-sha256', dry['source_sha256'],
                                          '--writer', 'a', '--expected-revision', str(cli.read(self.root)[3]['revision']), '--operation-id', flag[2:])
                        else:
                            flag = flags[schema]; dry = self.call('migrate', flag)
                            self.call('migrate', flag, '--apply', '--source-sha256', dry['source_sha256'],
                                      '--writer', 'a', '--expected-revision', '0', '--operation-id', 'm')
                    rev = int(cli.read(self.root)[3]['revision'])
                    error = self.write({'archive_decisions': ['d1']}, revision=rev, expect=2)
                    self.assertIn('migrate --to-latest', error['error'])
                finally:
                    self.root = saved

    def test_explicit_and_automatic_archive_do_not_duplicate(self):
        patch = big_patch()
        patch['archive_decisions'] = ['d1']
        next(t for t in patch['tasks'] if t['id'] == 'live')['acceptance'] = ['A', 'B', 'C']
        # d1 is an existing live decision; a distinct cold decision is auto-eligible.
        patch['decisions'] = [r for r in patch['decisions'] if r['id'] not in ('d1', 'd2')]
        result = self.write(patch)
        self.assertGreater(result['archive']['automatic'], 0)
        records = v4.collect_archive(cli.read_envelope(self.root)[4], self.root, cli.read(self.root)[3]['project_id'])
        ids = [r['id'] for r in records]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sum(r['collection'] == 'decisions' and r['record_id'] == 'd1' for r in records), 1)

    def test_capacity_diagnostics_and_validate_integrity(self):
        report = self.call('capacity')
        self.assertIn('decisions', report['current']['collections'])
        self.assertEqual(report['cold_archive']['count'], 0)
        self.assertEqual({r['id'] for r in report['decision_archival']['eligible']}, {'d1', 'd2'})
        self.assertNotIn('patch', report['decision_archival'])
        self.assertTrue(report['largest_records'])
        brief = self.call('status', '--brief')
        self.assertIn('headroom_bytes', brief['capacity'])
        self.assertEqual(brief['progress_summary']['handoff_hint'], 'handoff --task live --limit 5')
        checked = self.call('validate', '--verify-integrity')
        self.assertTrue(checked['checks']['integrity']['verified'])
        self.assertEqual(checked['checks']['integrity']['state'], 'verified')

    def test_mapping_reason_is_explicit(self):
        self.write({'extensions': {'migration': {'mapping_review_required': True}}})
        for command in (('status',), ('status', '--brief')):
            report = self.call(*command)
            self.assertTrue(report['mapping_review_required'])
            self.assertEqual(report['mapping_review_reason'], 'field_mapping_and_reader_cutover_pending')
            self.assertEqual(report['verification'], 'UNVERIFIED')

    def test_existing_cold_id_conflict_is_reported_not_read_rejected(self):
        _before, state = self.state()
        original = state['decisions'][0]
        self.write({'archive_decisions': ['d1']})
        text, lines, body, meta, stub, _matches = cli.read_envelope(self.root)
        stub['decisions'].append(original)
        # Simulate an older writer that allowed a hot/cold ID collision.
        (self.root / '.relay/CURRENT.md').write_text(cli.metadata(lines, {}) + p.render_body(stub, body))
        self.assertTrue(self.call('status')['document_valid'])
        self.assertEqual(self.call('capacity')['archived_decisions']['decision_id_conflicts'], ['d1'])
        self.write({'decisions': [original]}, revision=2, op='same-existing')

    def test_invalid_command_and_deep_corruption_fail_closed(self):
        before, _state = self.state()
        error = self.call('prune', '--writer', 'a', '--expected-revision', '1',
                          '--operation-id', 'bad', patch={'archive_decisions': ['d1']}, expect=2)
        self.assertIn('lease', error['error'])
        self.assertEqual(self.state()[0], before)
        self.write({'archive_decisions': ['d1']})
        archive_file = next((self.root / '.relay/objects/archive').rglob('*.json'))
        archive_file.write_bytes(archive_file.read_bytes() + b' ')
        self.call('validate', '--verify-integrity', expect=2)
        checked = self.call('handoff', '--verify-integrity')
        self.assertEqual(checked['checks']['integrity']['state'], 'degraded')
        self.assertFalse(checked['checks']['integrity']['verified'])


class ActiveCapacityTests(V6Case):
    def test_synthetic_active_task_capacity_and_receipt_batches(self):
        self.call('init', frozen=STAMP)
        decisions = [decision('long' + str(i), 600) for i in range(17)]
        for row in decisions:
            row['reason'] = 'r' * 750
        compact = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())
        decisions[-1]['reason'] += 'r' * (24515 - compact(decisions))
        self.assertEqual(compact(decisions), 24515)
        patch = {'project': {'current_task': 'live'},
                 'tasks': [{'id': 'live', 'title': 'Continuous work', 'status': 'doing', 'acceptance': ['A','B','C']}],
                 'decisions': decisions, 'extensions': {'context': 'x' * 500}}
        args = ('--acquire', '--writer', 'a', '--expected-revision', '0', '--operation-id', 'seed')
        preview = self.call('capacity', *args, patch=patch, frozen=STAMP)['patch_preview']
        patch['extensions']['context'] += 'x' * (32200 - preview['final_candidate_bytes'])
        self.call('save', *args, patch=patch, frozen=STAMP)
        original = (self.root / '.relay/CURRENT.md').read_text()
        report = self.call('capacity')
        self.assertEqual(report['current']['collections']['decisions']['bytes'], 24515)
        self.assertTrue(report['collection_hints'])
        self.assertEqual(report['long_records']['count'], 0)
        self.assertNotIn('conclusion', str(report['largest_records']))
        self.assertEqual(len(original.encode()), 32200)
        before = cli.resolve(original, self.root)
        large = {'decisions': [decision('new', 2000)]}
        self.call('save', '--acquire', '--writer', 'a', '--expected-revision', '1',
                  '--operation-id', 'fails', patch=large, expect=2, frozen=STAMP)
        archive_patch = {'archive_decisions': ['long' + str(i) for i in range(17)]}
        self.call('save', '--acquire', '--writer', 'a', '--expected-revision', '1',
                  '--operation-id', 'archive', patch=archive_patch, frozen=STAMP)
        archived = (self.root / '.relay/CURRENT.md').read_text()
        self.assertTrue(migrate_compare.compare(original, archived, self.root)[0])
        self.assertLess(len(archived.encode()), p.V4_COMPACTION_TRIGGER_BYTES)
        self.assertEqual(self.call('archive', '--id', 'long0')['records'][0]['record'], decisions[0])
        preview = self.call('capacity', '--acquire', '--writer', 'a', '--expected-revision', '2',
                            '--operation-id', 'new', patch=large, frozen=STAMP)['patch_preview']
        committed = self.call('save', '--acquire', '--writer', 'a', '--expected-revision', '2',
                              '--operation-id', 'new', patch=large, frozen=STAMP)
        self.assertEqual(preview['final_candidate_bytes'], committed['capacity']['used_bytes'])
        after = cli.resolve((self.root / '.relay/CURRENT.md').read_text(), self.root)
        self.assertEqual(before['tasks'], after['tasks'])
        self.assertEqual(before['project']['current_task'], after['project']['current_task'])
        self.assertEqual(self.call('handoff', '--task', 'live')['archived_decisions']['count'], 17)
        for revision in range(3, 11, 2):
            resume = self.call('resume', '--writer', 'a', '--expected-revision', str(revision),
                               '--operation-id', 'r' + str(revision), frozen=STAMP)
            save = self.call('save', '--writer', 'a', '--expected-revision', str(revision + 1),
                             '--operation-id', 's' + str(revision), frozen=STAMP)
            self.assertEqual(resume['compaction']['archived_receipts'], 0)
            self.assertEqual(save['compaction']['archived_receipts'], 0)
        self.assertTrue(self.call('validate', '--verify-integrity')['checks']['integrity']['verified'])
