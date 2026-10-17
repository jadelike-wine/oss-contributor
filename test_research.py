import copy
import json
import unittest
from unittest.mock import patch

from autonomous import Autonomous
from contribute import Pipeline
from research import needs_discussion, validate
from test_autonomous import AutonomousTests


def opportunity(repo='owner/repo'):
    return dict(decision='candidate', reason='Value mismatch', category='bug', risk='low',
        title='Value is wrong', background='Default value differs from documented contract',
        users='API callers', expected_effect='Correct output', scope='value.py and regression',
        validation='python test_value.py: assertion fails before repair and passes after',
        risks='Small default behavior change', effort='Small: one constant and a test',
        acceptance='Likely: documented behavior and bounded change', comparison='Peer behavior is context only',
        issue_body='Value differs from the expected result. Tests pending.',
        duplicate_queries=['specific_value_bug'],
        evidence=[{'repo': repo, 'path': 'value.py', 'quote': 'VALUE = 1'}],
        policy_evidence=[{'repo': repo, 'path': 'CONTRIBUTING.md', 'quote': 'Small bug fixes welcome.'}],
        policy={'automated_issue': True, 'automated_pr': True, 'maintainer_required': False,
                'reason': 'Small fixes allowed; automated contribution permitted'})


class ResearchTests(unittest.TestCase):
    # Reuse real Git fixtures and adapters, without inheriting their test cases.
    setUp = AutonomousTests.setUp
    tearDown = AutonomousTests.tearDown
    git = AutonomousTests.git
    model = AutonomousTests.model
    mocks = AutonomousTests.mocks
    fake_gh = AutonomousTests.fake_gh
    fake_command = AutonomousTests.fake_command
    def packet(self):
        (self.work / 'CONTRIBUTING.md').write_text('Small bug fixes welcome.\n', encoding='utf-8')
        self.git('add', 'CONTRIBUTING.md')
        self.git('commit', '-qm', 'policy')
        self.base = self.git('rev-parse', 'HEAD')
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET source='hunt',number=NULL,base_sha=? WHERE id=?", (self.base, self.key))
        return {'peers': [], 'policy': {'CONTRIBUTING.md': 'Small bug fixes welcome.'}}

    def research_mocks(self, answer, packet):
        stack = self.mocks()
        # Replace the legacy research stub with the real read-only research function.
        stack.enter_context(patch.object(self.engine, 'research', wraps=Autonomous.research.__get__(self.engine)))
        stack.enter_context(patch('research.collect', return_value=packet))
        def model(key, purpose, prompt, edit=False):
            if purpose == 'research':
                self.assertFalse(edit)
                return copy.deepcopy(answer)
            return self.model(key, purpose, prompt, edit)
        stack.enter_context(patch.object(self.engine, 'model', side_effect=model))
        return stack

    def test_low_risk_research_runs_existing_full_pipeline_and_resumes(self):
        packet = self.packet()
        with self.research_mocks(opportunity(), packet):
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
        self.assertEqual(1, len(self.created_issues))
        self.assertEqual(1, len(self.created_prs))
        result = json.loads((self.root / 'runs' / self.key / 'opportunity.json').read_text())
        self.assertTrue(result['approved'])
        self.assertIn(self.base, result['opportunity']['evidence'][0]['url'])

    def test_design_proposal_waits_before_any_test_or_repair_then_resumes(self):
        packet = self.packet()
        answer = opportunity()
        answer.update(risk='low', category='feature')
        packet['peers'] = [{'repo': 'peer/harness', 'sha': self.base, 'files': {'feature.py': 'VALUE = 2'}}]
        answer['evidence'].append({'repo': 'peer/harness', 'path': 'feature.py', 'quote': 'VALUE = 2'})
        with self.research_mocks(answer, packet):
            self.assertEqual('WAITING_MAINTAINER', self.engine.advance(self.key))
            self.assertEqual([], self.purposes)
            self.assertFalse((self.work / 'test_value.py').exists())
            self.assertNotIn('Executed reproduction', self.created_issues[0]['body'])
            self.assertIn('static; tests pending', self.created_issues[0]['body'])
            # Reopening the same DB must retain the proposal and publication identity.
            self.engine = Autonomous(self.p)
            data = self.engine.load(self.key)
            self.assertTrue(data['proposal_only'])
            self.comments = [{'author_association': 'MEMBER', 'body': 'Please implement.'}]
        with self.mocks():
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
        self.assertEqual(1, len(self.created_issues))

    def test_private_security_never_searches_or_publishes(self):
        packet = self.packet()
        with self.research_mocks({'decision': 'private', 'reason': 'Use private disclosure policy'}, packet), \
                patch.object(self.engine, 'duplicates') as duplicate:
            self.assertEqual('SKIPPED', self.engine.advance(self.key))
        duplicate.assert_not_called()
        self.assertEqual([], self.created_issues)

    def test_lost_proposal_response_recovers_without_duplicate_or_implementation(self):
        packet = self.packet()
        answer = opportunity()
        answer['risk'] = 'medium'
        self.lose_issue_response = True
        with self.research_mocks(answer, packet):
            self.assertEqual('ISSUE', self.engine.advance(self.key))
            self.assertEqual('WAITING_MAINTAINER', self.engine.advance(self.key))
        self.assertEqual(1, len(self.created_issues))
        self.assertEqual([], self.purposes)
        self.assertEqual([], self.created_prs)

    def test_stale_proposal_cannot_publish_and_unapproved_design_cannot_publish_pr(self):
        packet = self.packet()
        answer = opportunity()
        answer['risk'] = 'high'
        with self.research_mocks(answer, packet):
            data = self.engine.load(self.key)
            self.engine.plan(self.key, data)
            self.assertEqual('ISSUE', data['stage'])
            (self.work / 'value.py').write_text('VALUE = 9\n', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'unchanged independently approved research'):
                self.engine.publish_issue(self.key, data)
            with self.assertRaisesRegex(RuntimeError, 'requires maintainer approval'):
                self.engine.publish_pr(self.key, data)
        self.assertEqual([], self.created_issues)

    def test_cli_shares_policy_exception_with_automation(self):
        import shutil
        import subprocess
        import sys
        for name in ('contribute.py', 'autonomous.py', 'repository_policy.py', 'ci_status.py', 'telemetry.py'):
            shutil.copyfile(__import__('pathlib').Path(__file__).parent / name, self.root / name)
        script = ("import runpy,sys; sys.argv=['contribute.py','status']; "
                  "runpy.run_path('contribute.py', run_name='__main__'); import autonomous; "
                  "assert autonomous.ContributionPaused is sys.modules['contribute'].ContributionPaused")
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_policy_disallows_automatic_issue_without_consuming_retries(self):
        packet = self.packet()
        answer = opportunity()
        answer['policy']['automated_issue'] = False
        with self.research_mocks(answer, packet):
            self.assertEqual('PLAN', self.engine.advance(self.key))
        self.assertEqual({}, self.engine.load(self.key)['attempts'])
        self.assertEqual([], self.created_issues)
        self.assertFalse((self.work / 'test_value.py').exists())

    def test_maintainer_approval_cannot_override_automation_ban(self):
        packet = self.packet()
        answer = opportunity()
        answer['policy']['automated_pr'] = False
        with self.research_mocks(answer, packet):
            self.assertEqual('WAITING_MAINTAINER', self.engine.advance(self.key))
            self.comments = [{'author_association': 'MEMBER', 'body': 'Please implement.'}]
            self.assertEqual('PLAN', self.engine.advance(self.key))
            self.assertFalse((self.work / 'test_value.py').exists())
            self.assertEqual({}, self.engine.load(self.key)['attempts'])
            with self.assertRaisesRegex(RuntimeError, 'requires maintainer approval'):
                self.engine.publish_pr(self.key, self.engine.load(self.key))
        self.assertEqual([], self.created_prs)

    def test_research_rejects_fabricated_quote_and_target_path_escape(self):
        packet = self.packet()
        for path, quote in [('value.py', 'VALUE = 42'), ('../value.py', 'VALUE = 1')]:
            answer = opportunity()
            answer['evidence'][0].update(path=path, quote=quote)
            with self.assertRaisesRegex(ValueError, 'does not match source'):
                validate(answer, self.p.task(self.key), packet)

    def test_feature_requires_peer_implementation_and_discussion(self):
        packet = self.packet()
        answer = opportunity()
        answer['category'] = 'feature'
        self.assertTrue(needs_discussion(answer))
        with self.assertRaisesRegex(ValueError, 'actual peer implementation'):
            validate(answer, self.p.task(self.key), packet)

    def test_duplicate_stops_research_before_test_edits(self):
        packet = self.packet()
        with self.research_mocks(opportunity(), packet), patch.object(self.engine, 'duplicates', return_value=False):
            self.assertEqual('SKIPPED', self.engine.advance(self.key))
        self.assertEqual([], self.created_issues)
        self.assertEqual([], self.purposes)

    def test_diagnostic_hunt_analyze_is_read_only_and_consumable_by_plan(self):
        packet = self.packet()
        with self.research_mocks(opportunity(), packet), patch('autonomous.Autonomous', return_value=self.engine):
            result = self.p.analyze(self.key)
        self.assertTrue(result.endswith('opportunity.json'))
        self.assertEqual('DISCOVERED', self.p.task(self.key)['state'])
        self.assertEqual('PLAN', self.engine.load(self.key)['stage'])
        self.assertTrue(self.engine.load(self.key)['research']['approved'])
        self.assertEqual([], self.created_issues)


del AutonomousTests


if __name__ == '__main__':
    unittest.main()
