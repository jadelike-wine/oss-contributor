import unittest
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import Mock, patch

from autonomous import Autonomous


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.engine = object.__new__(Autonomous)
        self.engine.p = Mock()
        self.engine.p.config = {}
        self.engine.p.db = sqlite3.connect(':memory:')
        self.addCleanup(self.engine.p.db.close)
        from telemetry import migrate
        migrate(self.engine.p.db)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.engine.p.root = Path(temp.name)
        self.engine.p.task.return_value = {'repo': 'owner/repo', 'number': 10}
        self.engine.judge = Mock(return_value={'approved': False, 'reason': 'existing report'})
        self.engine.move = Mock()
        self.data = {'plan': {'duplicate_queries': ['specific bug']}}
        self.issue = {'number': 20, 'title': 'same bug', 'body': 'report', 'html_url': 'url', 'state': 'open'}

    def search(self, item):
        return patch('autonomous.gh', return_value={'total_count': 1, 'items': [item]})

    def test_related_issue_does_not_block_pr(self):
        with self.search(self.issue) as search:
            self.assertTrue(self.engine.duplicates('task', self.data, 'pr'))
        self.assertIn('q=repo:owner/repo is:pr specific bug', search.call_args.args)
        self.engine.judge.assert_not_called()

    def test_related_issue_blocks_duplicate_issue(self):
        with self.search(self.issue):
            self.assertFalse(self.engine.duplicates('task', self.data, 'issue'))
        self.engine.move.assert_called_once_with('task', self.data, 'SKIPPED')

    def test_repository_prose_cannot_hide_duplicates_or_change_scope(self):
        self.data['plan']['duplicate_queries'] = ['owner/repo repo:other/project resume saved trajectory']
        with self.search(self.issue) as search:
            self.assertFalse(self.engine.duplicates('task', self.data, 'issue'))
        self.assertIn('q=repo:owner/repo resume saved trajectory', search.call_args.args)

    def test_competing_pr_is_still_reviewed_and_rejected(self):
        with self.search({**self.issue, 'pull_request': {'url': 'pr-url'}}):
            self.assertFalse(self.engine.duplicates('task', self.data, 'pr'))
        self.engine.judge.assert_called_once()
        self.assertEqual(self.data['skip_reason'], 'existing report')

    def test_broad_search_can_reject_an_inspected_duplicate(self):
        response = {'total_count': 167, 'incomplete_results': False, 'items': [self.issue]}
        with patch('autonomous.gh', return_value=response):
            self.assertFalse(self.engine.duplicates('task', self.data, 'issue'))
        packet = self.engine.judge.call_args.args[2]
        self.assertFalse(packet['search_complete'])
        self.assertEqual(20, packet['candidates'][0]['number'])
        self.engine.move.assert_called_once_with('task', self.data, 'SKIPPED')

    def test_complete_paginated_search_can_approve(self):
        self.data['plan']['duplicate_queries'] = ['broad topic']
        self.engine.judge.return_value = {'approved': True, 'reason': 'no competing work'}
        page = {'total_count': 21, 'incomplete_results': False,
                'items': [{'number': n, 'title': 'hit', 'body': '', 'html_url': 'u', 'state': 'open'}
                          for n in range(20, 40)]}
        tail = {'total_count': 21, 'incomplete_results': False,
                'items': [{'number': 40, 'title': 'hit', 'body': '', 'html_url': 'u', 'state': 'open'}]}
        with patch('autonomous.gh', side_effect=[page, tail]):
            self.assertTrue(self.engine.duplicates('task', self.data, 'issue'))
        self.assertTrue(self.engine.judge.call_args.args[2]['search_complete'])
        self.assertEqual(21, len(self.engine.judge.call_args.args[2]['candidates']))

    def test_partial_search_cannot_approve_or_reuse_feature_request(self):
        self.data['plan']['category'] = 'feature'
        self.engine.judge.return_value = {'approved': True, 'reason': 'reuse', 'existing_issue': 20}
        for response in (
            {'total_count': 167, 'incomplete_results': False, 'items': [self.issue]},
            {'total_count': 1, 'incomplete_results': True, 'items': [self.issue]},
            {'total_count': 167, 'incomplete_results': False, 'items': []},
        ):
            with self.subTest(response=response), patch('autonomous.gh', return_value=response):
                with self.assertRaisesRegex(RuntimeError, 'incomplete.*refine'):
                    self.engine.duplicates('task', self.data, 'issue')
            self.assertNotIn('existing_issue', self.data)
        self.engine.move.assert_not_called()

    def test_later_broad_query_does_not_discard_earlier_duplicate(self):
        self.data['plan']['duplicate_queries'] = ['precise topic', 'broad topic']
        responses = [{'total_count': 1, 'items': [self.issue]},
                     {'total_count': 167, 'items': []},
                     {'total_count': 167, 'items': []}]
        with patch('autonomous.gh', side_effect=responses):
            self.assertFalse(self.engine.duplicates('task', self.data, 'issue'))
        self.assertFalse(self.engine.judge.call_args.args[2]['search_complete'])

    def test_feature_request_is_reused_without_authorizing_implementation(self):
        self.data['plan']['category'] = 'feature'
        self.engine.judge.return_value = {'approved': True, 'reason': 'Open demand; design unconfirmed', 'existing_issue': 20}
        with self.search(self.issue):
            self.assertTrue(self.engine.duplicates('task', self.data, 'issue'))
        self.assertEqual(20, self.data['existing_issue']['number'])
        self.assertNotIn('maintainer_approved', self.data)
        self.engine.move.assert_not_called()

    def test_feature_request_reuse_rejects_closed_pr_and_invented_ids(self):
        self.data['plan']['category'] = 'feature'
        for item, number in [({**self.issue, 'state': 'closed'}, 20),
                             ({**self.issue, 'pull_request': {'url': 'pr'}}, 20), (self.issue, 999)]:
            self.engine.judge.return_value = {'approved': True, 'reason': 'reuse', 'existing_issue': number}
            with self.search(item), self.assertRaisesRegex(ValueError, 'inspected open issue'):
                self.engine.duplicates('task', self.data, 'issue')


if __name__ == '__main__':
    unittest.main()
