"""Priority and publication regressions; GitHub is mocked, Git uses temp repositories."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_autonomous as fixture
import test_research as research_fixture
import test_cross_harness_flow as matrix_fixture
import test_scan as scan_fixture
from autonomous import Autonomous
from contribute import ContributionPaused
from feature_matrix import route
from research import needs_discussion


STRATEGY = {'prefer_own_pr_on_competition': True, 'submit_issue_and_pr_together': True,
            'link_pr_in_own_issue': True}


class ContributionStrategyTests(unittest.TestCase):
    setUp = fixture.AutonomousTests.setUp
    tearDown = fixture.AutonomousTests.tearDown
    git = fixture.AutonomousTests.git
    model = fixture.AutonomousTests.model
    mocks = fixture.AutonomousTests.mocks
    fake_gh = fixture.AutonomousTests.fake_gh
    fake_command = fixture.AutonomousTests.fake_command
    research_mocks = research_fixture.ResearchTests.research_mocks
    packet = research_fixture.ResearchTests.packet
    legacy_packet = research_fixture.ResearchTests.packet

    def enable(self, link=False):
        self.p.config['contribution_strategy'] = {**STRATEGY, 'link_pr_in_own_issue': link}

    def external_issue(self):
        key = 'owner--repo--1'
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET id=? WHERE id=?', (key, self.key))
        self.key = key
        return {'number': 1, 'state': 'open', 'title': 'Value mismatch', 'user': {'login': 'bot'},
                'body': 'Original report, evidence and user edits.', 'comments_data': []}

    def test_feature_can_submit_issue_and_pr_same_run_with_matrix_and_no_approval(self):
        self.enable()
        packet, answer = matrix_fixture.CrossHarnessFlowTests.packet(self)
        with self.research_mocks(answer, packet):
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
        data = self.engine.load(self.key)
        self.assertFalse(data.get('maintainer_approved'))
        self.assertNotIn('maintainer_approval', data)
        self.assertEqual(1, len(self.created_issues))
        self.assertEqual(1, len(self.created_prs))
        self.assertEqual('READY_TO_IMPLEMENT', self.p.db.execute('SELECT queue FROM research_candidates').fetchone()[0])

    def test_own_discovery_competing_pr_does_not_cancel_real_red_green_pipeline(self):
        self.enable()
        packet = self.packet()
        competing = {'number': 99, 'title': 'Other fix', 'html_url': 'https://github.com/owner/repo/pull/99',
                     'pull_request': {'url': 'pr'}, 'state': 'open', 'body': 'Same value mismatch'}
        def gh(*args):
            if '-X' in args:
                return {'total_count': 1, 'items': [competing], 'incomplete_results': False}
            return self.fake_gh(*args)
        def judge(key, purpose, context):
            if purpose.startswith('duplicates-'):
                return {'approved': False, 'reason': 'Only a competing PR', 'competition_only': True, 'competing_prs': [99]}
            return {'approved': True, 'reason': 'Evidence and checks support the work'}
        with self.research_mocks(research_fixture.opportunity(), packet), \
                patch('autonomous.gh', side_effect=gh), patch.object(self.engine, 'judge', side_effect=judge):
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
        self.assertEqual(99, self.engine.load(self.key)['competing_prs'][0]['number'])
        self.assertEqual(1, len(self.created_prs))
        verification = json.loads((self.engine.packet_dir(self.key) / 'verification.json').read_text())
        self.assertTrue(verification['passed'])

    def test_numbered_own_issue_recognized_from_authenticated_author(self):
        self.enable()
        issue = self.external_issue()
        data = self.engine.load(self.key)
        with patch('autonomous.gh', side_effect=lambda *args: {'login': 'BOT'} if args[-1] == 'user' else issue):
            self.assertTrue(self.engine.own_contribution(self.key, data))
            issue['user']['login'] = 'somebody-else'
            self.assertFalse(self.engine.own_contribution(self.key, data))

    def test_numbered_own_issue_competing_pr_does_not_block_submission(self):
        self.enable()
        issue = self.external_issue()
        competing = {'number': 99, 'title': 'Other fix', 'html_url': 'url', 'pull_request': {'url': 'pr'}}
        def gh(*args):
            if args == ('api', 'repos/owner/repo/issues/1'):
                return issue
            if '-X' in args:
                return {'total_count': 1, 'items': [competing]}
            return self.fake_gh(*args)
        def judge(key, purpose, context):
            if purpose == 'duplicates-pr':
                return {'approved': False, 'reason': 'Competing PR', 'competition_only': True, 'competing_prs': [99]}
            return {'approved': True, 'reason': 'Checks and evidence support it'}
        with self.mocks(), patch('autonomous.gh', side_effect=gh), patch.object(self.engine, 'judge', side_effect=judge):
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
        self.assertEqual(1, len(self.created_prs))
        self.assertEqual([], self.created_issues)

    def test_own_closed_issue_can_resume_without_inventing_acceptance(self):
        self.enable()
        issue = self.external_issue()
        issue['state'] = 'closed'
        data = {'stage': 'WAITING_MAINTAINER', 'attempts': {}, 'proposal_only': True}
        with patch.object(self.engine, 'live_issue', return_value=issue), patch('autonomous.gh', return_value={'login': 'bot'}):
            self.assertTrue(self.engine.wait_maintainer(self.key, data))
        self.assertEqual('PLAN', data['stage'])
        self.assertFalse(data.get('maintainer_approved'))

    def test_explicit_policy_and_high_risk_still_wait(self):
        answer = research_fixture.opportunity()
        answer['category'] = 'feature'
        gates = {'reviewed': True, 'duplicate_clear': True, 'proactive': True}
        self.assertFalse(needs_discussion(answer, proactive=True))
        for key, value in [('maintainer_required', True), ('automated_pr', False)]:
            blocked = copy.deepcopy(answer)
            blocked['policy'][key] = value
            self.assertTrue(needs_discussion(blocked, proactive=True))
            self.assertEqual('NEEDS_MAINTAINER_CONFIRMATION', route(blocked, None, **gates)[0])
        answer['risk'] = 'high'
        self.assertTrue(needs_discussion(answer, proactive=True))
        for policy in ({'human_pr_review_required': True}, {'manual_issue_submission': True}):
            self.assertEqual('PAUSED_POLICY', route(answer, None, policy=policy, **gates)[0])

    def test_real_reviewer_prompt_receives_our_publication_preference(self):
        self.enable()
        with patch.object(self.engine, 'hermes_response', return_value={'approved': True, 'reason': 'Checked'}) as response:
            self.engine.judge(self.key, 'auto-review', {'gate': 'Review this patch'})
        prompt = response.call_args.args[2]
        self.assertIn('a declined proposal', prompt)
        self.assertIn('Explicit upstream approval/label/automation/human-review rules', prompt)

    def test_partial_competition_search_still_cannot_authorize_publication(self):
        self.enable()
        data = {'plan': self.plan}
        competing = {'number': 99, 'title': 'Other fix', 'html_url': 'https://github.com/owner/repo/pull/99',
                     'pull_request': {'url': 'pr'}, 'state': 'open'}
        with patch('autonomous.gh', return_value={'total_count': 1, 'items': [competing], 'incomplete_results': True}), \
                patch.object(self.engine, 'judge', return_value={'approved': False, 'reason': 'Competition',
                    'competition_only': True, 'competing_prs': [99]}):
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                self.engine.duplicates(self.key, data, 'pr')
        self.assertEqual([], self.created_prs)

    def test_other_rejection_cannot_be_overridden_by_competing_pr(self):
        self.enable()
        data = {'plan': self.plan}
        competing = {'number': 99, 'title': 'Other fix', 'html_url': 'url', 'pull_request': {'url': 'pr'}}
        with patch('autonomous.gh', return_value={'total_count': 1, 'items': [competing]}), \
                patch.object(self.engine, 'judge', return_value={'approved': False, 'reason': 'Baseline already fixed', 'competing_prs': [99]}):
            self.assertFalse(self.engine.duplicates(self.key, data, 'pr'))
        self.assertEqual('SKIPPED', data['stage'])

    def test_issue_body_backlink_preserves_text_is_idempotent_and_recovers_lost_response(self):
        self.enable(link=True)
        issue = self.external_issue()
        data = {'stage': 'PR_OPEN', 'attempts': {}, 'pr_number': 3, 'pr_url': 'https://github.com/owner/repo/pull/3'}
        writes = []
        def write(args, **kwargs):
            self.assertEqual(['gh', 'issue', 'edit', '1'], args[:4])
            issue['body'] = Path(args[args.index('--body-file') + 1]).read_text(encoding='utf-8')
            writes.append(args)
            raise RuntimeError('Lost response after the update was accepted')
        with patch('autonomous.gh', side_effect=lambda *args: {'login': 'bot'} if args[-1] == 'user' else issue), \
                patch.object(self.p, 'require_contribution'), patch('autonomous.command', side_effect=write):
            with self.assertRaisesRegex(RuntimeError, 'Lost response'):
                self.engine.link_pr_in_issue(self.key, data)
            self.engine = Autonomous(self.p)
            data = self.engine.load(self.key)
            self.engine.link_pr_in_issue(self.key, data)
            self.engine.link_pr_in_issue(self.key, data)
        self.assertEqual(1, len(writes))
        self.assertIn('Original report, evidence and user edits.', issue['body'])
        self.assertEqual(1, issue['body'].count('## Our implementation'))
        self.assertNotIn('issue_pr_link_pending', data)
        self.assertEqual(data['pr_url'], data['issue_pr_link']['pr_url'])

    def test_no_issue_edit_for_other_author_or_without_external_write_authorization(self):
        self.enable(link=True)
        issue = self.external_issue()
        data = {'stage': 'PR_OPEN', 'attempts': {}, 'pr_number': 3, 'pr_url': 'https://github.com/owner/repo/pull/3'}
        with patch('autonomous.gh', side_effect=lambda *args: {'login': 'bot'} if args[-1] == 'user' else issue), \
                patch.object(self.p, 'require_contribution'), patch('autonomous.command') as write:
            issue['user']['login'] = 'other'
            self.engine.link_pr_in_issue(self.key, data)
            write.assert_not_called()
            issue['user']['login'] = 'bot'
            self.p.allow_external_writes = False
            with self.assertRaises(ContributionPaused):
                self.engine.link_pr_in_issue(self.key, data)
            write.assert_not_called()
        self.assertEqual('Original report, evidence and user edits.', issue['body'])

    def test_link_failure_does_not_turn_open_pr_into_a_failed_publication(self):
        self.enable(link=True)
        data = {'stage': 'PR_OPEN', 'attempts': {}, 'pr_number': 3, 'pr_url': 'https://github.com/owner/repo/pull/3'}
        with patch.object(self.engine, 'link_pr_in_issue', side_effect=RuntimeError('temporary disconnect')):
            self.engine.sync_issue_link(self.key, data)
        self.assertEqual('PR_OPEN', self.engine.load(self.key)['stage'])
        self.assertEqual(3, self.engine.load(self.key)['pr_number'])
        self.assertTrue(data['issue_pr_link_pending'])


class OwnIssueScanTests(unittest.TestCase):
    setUp = scan_fixture.ScanTests.setUp
    tearDown = scan_fixture.ScanTests.tearDown
    fake_gh = scan_fixture.ScanTests.fake_gh
    scan = scan_fixture.ScanTests.scan

    def test_scan_competing_pr_holds_others_but_keeps_our_report_eligible(self):
        repos = self.config['repositories'][:2]
        self.p.config['repositories'] = repos
        for repo in repos:
            self.prs[repo] = [{'number': 99, 'body': 'Fixes #12', 'url': f'https://github.com/{repo}/pull/99'}]
        self.issues[repos[0]][0]['author'] = {'login': 'bot'}
        self.issues[repos[1]][0]['author'] = {'login': 'other'}
        def gh(*args):
            return {'login': 'bot'} if args == ('api', 'user') else self.fake_gh(*args)
        self.scan(gh=gh)
        self.assertEqual('DISCOVERED', self.p.task(repos[0].replace('/', '--') + '--12')['state'])
        self.assertEqual('HOLD', self.p.task(repos[1].replace('/', '--') + '--12')['state'])


if __name__ == '__main__':
    unittest.main()
