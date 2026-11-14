"""PR-First regression: the gemini-cli #29864 scenario must never repeat.

Our own issue was parked in WAITING_MAINTAINER for 22 days because the issue
lacked a 'help wanted' label; a maintainer then merged someone else's five-hour
PR and closed it. These tests pin the new behavior end to end:

1. Own issue + maintainer_required policy (no help wanted) -> implement + PR.
2. A WAITING_MAINTAINER task escalates to implementation after the wait budget.
3. Soft policy signals never produce an unbounded wait.
4. The unified policy module keeps hard gates hard and soft gates soft.
"""
import copy
import unittest
from unittest.mock import patch

from autonomous import Autonomous
from contribute import ContributionPaused
from feature_matrix import route
from policy_intelligence import (assess, needs_discussion, wait_escalation,
                                 DEFAULT_WAIT_TIMEOUT_SECONDS)
from research import needs_discussion as research_discussion
from test_autonomous import AutonomousTests as Fixture
from test_research import opportunity


class PolicyIntelligenceTests(unittest.TestCase):
    def test_hard_and_soft_classification(self):
        opportunity_ = opportunity()
        # Soft conventions never wait.
        for soft in ({'maintainer_required': True}, {'automated_pr': False},
                     {'automated_issue': False}, {'discussion_required': True}):
            policy = {**opportunity_['policy'], **soft}
            answer = {**opportunity_, 'policy': policy}
            self.assertFalse(needs_discussion(answer), soft)
            self.assertFalse(research_discussion(answer), soft)
        # Hard gates still wait.
        for hard in ({'automation': 'forbidden'}, {'automation': 'disallowed'},
                     {'automation': 'requires_explicit_maintainer_instructions'},
                     {'manual_pr_submission': True},
                     {'pr_submission': 'maintainer_authorization_required'}):
            policy = {**opportunity_['policy'], **hard}
            answer = {**opportunity_, 'policy': policy}
            self.assertTrue(needs_discussion(answer), hard)
        # Security category and high risk remain hard.
        self.assertTrue(needs_discussion({**opportunity_, 'category': 'security'}))
        self.assertTrue(needs_discussion({**opportunity_, 'risk': 'high'}))

    def test_acceptance_score_degrades_not_blocks(self):
        issue = {'labels': [], 'assignees': [{'login': 'other'}],
                 'state': 'open', 'number': 1,
                 'html_url': 'https://github.com/o/r/issues/1'}
        verdict = assess({'maintainer_required': True, 'automated_pr': False},
                         issue=issue, actor='me')
        self.assertEqual('proceed_degraded', verdict['verdict'])
        self.assertTrue(verdict['proceed'])
        self.assertLess(verdict['acceptance_score'], 100)
        self.assertFalse(verdict['hard_blocks'])
        self.assertTrue(verdict['soft_penalties'])

    def test_enforced_label_becomes_hard(self):
        issue = {'labels': [], 'assignees': [], 'state': 'open', 'number': 1,
                 'html_url': 'https://github.com/o/r/issues/1'}
        verdict = assess({'required_issue_labels': ['ready'],
                          'required_issue_labels_enforced': True},
                         issue=issue, actor='me')
        self.assertEqual('blocked', verdict['verdict'])

    def test_route_soft_policy_reaches_implementation_degraded(self):
        answer = opportunity()
        answer['policy']['maintainer_required'] = True
        answer['policy']['automated_pr'] = False
        queue, priority, reason = route(answer, None, reviewed=True, duplicate_clear=True)
        self.assertEqual('READY_TO_IMPLEMENT', queue)
        self.assertIn('proceed_degraded', reason)
        self.assertLess(priority, 70)

    def test_route_hard_policy_stays_paused(self):
        answer = opportunity()
        answer['policy']['automation'] = 'forbidden'
        queue, _, _ = route(answer, None, reviewed=True, duplicate_clear=True)
        self.assertEqual('PAUSED_POLICY', queue)

    def test_wait_escalation_clock(self):
        self.assertIsNone(wait_escalation({'waiting_since': '2026-11-14T00:00:00+00:00'},
                                          now_iso='2026-11-14T01:00:00+00:00'))
        overdue = {'waiting_since': '2026-11-10T00:00:00+00:00'}
        self.assertEqual('PLAN', wait_escalation(
            overdue, now_iso='2026-11-14T00:00:00+00:00'))
        # Custom timeout honored.
        self.assertIsNone(wait_escalation(
            overdue, now_iso='2026-11-11T00:00:00+00:00',
            option_timeout=DEFAULT_WAIT_TIMEOUT_SECONDS * 10))
        # Missing clock escalates immediately (legacy records).
        self.assertEqual('PLAN', wait_escalation({}, now_iso='2026-11-14T00:00:00+00:00'))


class Issue29864RegressionTests(unittest.TestCase):
    """End-to-end: own bug issue must ship issue+PR, never park forever."""

    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    git = Fixture.git
    model = Fixture.model
    mocks = Fixture.mocks
    fake_gh = Fixture.fake_gh
    fake_command = Fixture.fake_command

    def test_own_issue_with_soft_policy_ships_pr_immediately(self):
        """gemini-cli #29864: maintainer_required + no help wanted must not wait."""
        with self.mocks():
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
        self.assertEqual(1, len(self.created_prs))

    def test_research_candidate_with_soft_policy_ships_pr(self):
        data = {'stage': 'PLAN', 'attempts': {},
                'research': {'approved': True,
                             'opportunity': {**opportunity(),
                                             'policy': {'automated_issue': True, 'automated_pr': False,
                                                        'maintainer_required': True,
                                                        'reason': 'help wanted convention'}}}}
        self.engine.store(self.key, data)
        with self.mocks():
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
        self.assertEqual(1, len(self.created_prs))

    def test_waiting_task_escalates_after_timeout(self):
        """A WAITING_MAINTAINER task must not wait forever."""
        data = {'stage': 'WAITING_MAINTAINER', 'attempts': {}, 'proposal_only': True,
                'waiting_since': '2026-01-01T00:00:00+00:00',
                'plan': self.plan}
        self.engine.store(self.key, data, 'WAITING_MAINTAINER')
        with self.mocks(), patch('autonomous.now', return_value='2026-11-14T00:00:00+00:00'):
            stage = self.engine.advance(self.key)
        self.assertNotEqual('WAITING_MAINTAINER', stage)

    def test_hard_ban_still_pauses_publication(self):
        data = {'stage': 'PUBLISH', 'attempts': {},
                'research': {'approved': True,
                             'opportunity': {**opportunity(),
                                             'policy': {'automated_issue': True, 'automated_pr': True,
                                                        'maintainer_required': False,
                                                        'automation': 'forbidden',
                                                        'reason': 'no bots'}}}}
        self.engine.store(self.key, data)
        with self.mocks():
            with self.assertRaises(ContributionPaused):
                self.engine.publish_pr(self.key, self.engine.load(self.key))
        self.assertEqual([], self.created_prs)


del Fixture

if __name__ == '__main__':
    unittest.main()
