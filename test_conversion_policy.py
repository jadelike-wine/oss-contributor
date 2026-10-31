"""Policy regressions from the October 31 contribution audit (no external writes)."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autonomous import Autonomous
from contribute import ContributionPaused, Pipeline
from repository_policy import (approval_receipt, contribution_constraints,
                               has_scope_approval, human_maintainer_comments, issue_policy_reason)


AGNO_RULE = ('Only start work once a maintainer has added the `ready` label. '
             'PRs whose linked issue is not labeled `ready` are closed automatically.')


class PolicyEvidenceTests(unittest.TestCase):
    def test_live_rule_adds_to_configured_gates_without_relaxing_them(self):
        configured = {'required_issue_labels': ['Reviewed'], 'ready_label': 'accepted', 'manual_issue_submission': True}
        policy = contribution_constraints(AGNO_RULE, configured)
        self.assertEqual(['accepted', 'ready', 'reviewed'], policy['required_issue_labels'])
        self.assertTrue(policy['manual_issue_submission'])
        self.assertEqual(['Reviewed'], configured['required_issue_labels'])

    def test_informal_label_mention_is_not_an_automatic_gate(self):
        self.assertEqual([], contribution_constraints('Use the `ready` label if useful.')['required_issue_labels'])

    def test_ready_is_a_gate_not_maintainer_approval(self):
        policy = contribution_constraints(AGNO_RULE)
        self.assertIn('ready', issue_policy_reason({'state': 'open', 'labels': [{'name': 'bug'}]}, policy))
        self.assertIsNone(issue_policy_reason({'state': 'open', 'labels': [{'name': 'Ready'}]}, policy))
        self.assertFalse(has_scope_approval({'maintainer_approved': True, 'labels': ['ready']}))
        self.assertIn('OPEN', issue_policy_reason({'state': 'closed', 'labels': [{'name': 'ready'}]}, policy))
        self.assertIn('Cannot verify', issue_policy_reason({'state': 'open'}, policy))

    def test_only_a_reviewed_human_comment_can_make_an_approval_receipt(self):
        human = {'id': 1, 'author_association': 'MEMBER', 'user': {'type': 'User', 'login': 'maintainer'}, 'body': 'Please implement this scope.'}
        bot = {**human, 'id': 2, 'user': {'type': 'Bot', 'login': 'bot'}}
        disguised_bot = {**human, 'id': 3, 'user': {'type': 'User', 'login': 'triage[bot]'}}
        unknown = {**human, 'id': 4, 'user': {'login': 'unknown'}}
        public = {**human, 'id': 5, 'author_association': 'CONTRIBUTOR'}
        self.assertEqual([human], human_maintainer_comments([human, bot, disguised_bot, unknown, public]))
        with self.assertRaises(ValueError):
            approval_receipt([bot], {'approved': True}, '2026-10-31T00:00:00+00:00')
        with self.assertRaises(ValueError):
            approval_receipt([human], {'approved': False}, '2026-10-31T00:00:00+00:00')
        receipt = approval_receipt([human, bot], {'approved': True, 'reason': 'Exact scope confirmed'}, '2026-10-31T00:00:00+00:00')
        self.assertTrue(has_scope_approval({'maintainer_approval': receipt}))
        self.assertEqual([1], receipt['comment_ids'])


class PolicyConversionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'config.json').write_text(json.dumps({
            'repositories': ['agno-agi/agno'], 'automation': {'wait_maintainer_days': 7},
        }), encoding='utf-8')
        self.p = Pipeline(root)
        self.addCleanup(self.p.db.close)
        self.p._repository_status = {'agno-agi/agno': {'active': True, 'contribute': True}}
        self.p._repository_evidence = {'agno-agi/agno': ({}, AGNO_RULE)}
        self.key = self.p.hunt('agno-agi/agno', 'Claude tool call IDs')
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET number=10887,source='issue' WHERE id=?", (self.key,))
        self.engine = Autonomous(self.p)
        self.data = {'stage': 'WAITING_MAINTAINER', 'attempts': {}, 'plan': {'category': 'bug'}}
        self.issue = {'state': 'open', 'number': 10887, 'title': 'Claude tool call IDs',
                      'labels': [{'name': 'bug'}], 'comments_data': []}

    def test_current_repo_ready_rule_blocks_bug_before_work(self):
        with patch('contribute.gh', return_value=self.issue):
            with self.assertRaisesRegex(ContributionPaused, 'ready'):
                self.p.require_contribution(self.p.task(self.key))

    def test_current_repo_ready_rule_blocks_legacy_fast_path_approval(self):
        self.data['maintainer_approved'] = True  # Historical fast-path record is not authorization.
        with patch('contribute.gh', return_value=self.issue), patch('contribute.inspect_repository', return_value=({}, '', AGNO_RULE, None, None)):
            with self.assertRaisesRegex(ContributionPaused, 'ready'):
                self.p.require_contribution(self.p.task(self.key), publishing=True)

    def test_bug_label_never_becomes_maintainer_approval(self):
        self.p._repository_evidence['agno-agi/agno'] = ({}, 'Contributions welcome.')
        with patch.object(self.engine, 'live_issue', return_value=self.issue):
            self.engine.wait_maintainer(self.key, self.data)
        self.assertIsNot(self.data.get('maintainer_approved'), True)

    def test_waiting_elapsed_never_becomes_maintainer_approval(self):
        self.p._repository_evidence['agno-agi/agno'] = ({}, 'Contributions welcome.')
        self.issue['labels'] = []
        self.data['waiting_since'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()
        with patch.object(self.engine, 'live_issue', return_value=self.issue):
            self.engine.wait_maintainer(self.key, self.data)
        self.assertIsNot(self.data.get('maintainer_approved'), True)

    def test_repository_member_bot_is_not_an_approval_source(self):
        self.p._repository_evidence['agno-agi/agno'] = ({}, 'Contributions welcome.')
        self.issue['labels'] = []
        self.issue['comments_data'] = [{'id': 1, 'author_association': 'MEMBER',
                                       'user': {'login': 'triage[bot]', 'type': 'Bot'},
                                       'body': 'Please proceed.'}]
        with patch.object(self.engine, 'live_issue', return_value=self.issue), \
                patch.object(self.engine, 'judge', return_value={'approved': True, 'reason': 'approved'}) as judge:
            self.engine.wait_maintainer(self.key, self.data)
        judge.assert_not_called()
        self.assertIsNot(self.data.get('maintainer_approved'), True)


if __name__ == '__main__':
    unittest.main()
