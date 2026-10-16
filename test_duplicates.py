import unittest
from unittest.mock import Mock, patch

from autonomous import Autonomous


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        self.engine = object.__new__(Autonomous)
        self.engine.p = Mock()
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

    def test_competing_pr_is_still_reviewed_and_rejected(self):
        with self.search({**self.issue, 'pull_request': {'url': 'pr-url'}}):
            self.assertFalse(self.engine.duplicates('task', self.data, 'pr'))
        self.engine.judge.assert_called_once()
        self.assertEqual(self.data['skip_reason'], 'existing report')


if __name__ == '__main__':
    unittest.main()
