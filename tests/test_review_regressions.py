"""Targeted audit regressions with synthetic local state only."""
from test_v6_archive import V6Case
import cli_v2 as cli


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
