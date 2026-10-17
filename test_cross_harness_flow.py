import copy
import json
import subprocess
import unittest
from unittest.mock import patch

from autonomous import Autonomous
from telemetry import BudgetExhausted, metrics, record_usage, run
from test_autonomous import AutonomousTests as Fixture
from test_research import ResearchTests as ResearchFixture
from test_feature_matrix import matrix_answer


class CrossHarnessFlowTests(unittest.TestCase):
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    git = Fixture.git
    model = Fixture.model
    mocks = Fixture.mocks
    fake_gh = Fixture.fake_gh
    fake_command = Fixture.fake_command
    research_mocks = ResearchFixture.research_mocks
    legacy_packet = ResearchFixture.packet

    def packet(self):
        legacy = self.legacy_packet()
        task = self.p.task(self.key)
        peers = ['peer/one', 'peer/two', 'peer/three']
        packet = {**legacy, 'schema_version': 2,
                  'target': {'repo': task['repo'], 'sha': task['base_sha'],
                             'files': {'value.py': 'VALUE = 1', **legacy['policy']}},
                  'peers': [{'repo': p, 'sha': str(i) * 40, 'files': {'feature.py': 'VALUE = 2'}} for i, p in enumerate(peers, 1)]}
        answer = matrix_answer(task, peers)
        answer['evidence'].append({'repo': peers[0], 'path': 'feature.py', 'quote': 'VALUE = 2'})
        return packet, answer

    def test_feature_matrix_to_confirmation_then_real_local_red_green_and_mock_pr(self):
        packet, answer = self.packet()
        with self.research_mocks(answer, packet):
            self.assertEqual(self.engine.advance(self.key), 'WAITING_MAINTAINER')
            self.assertFalse((self.work / 'test_value.py').exists())
            self.assertEqual(self.p.db.execute('SELECT queue FROM research_candidates').fetchone()[0], 'NEEDS_MAINTAINER_CONFIRMATION')
            self.comments = [{'author_association': 'MEMBER', 'body': 'Please implement.'}]
            self.assertEqual(self.engine.advance(self.key), 'PR_OPEN')
        self.assertEqual(len(self.created_prs), 1)
        self.assertEqual(self.p.db.execute('SELECT queue FROM research_candidates').fetchone()[0], 'READY_TO_IMPLEMENT')
        verification = json.loads((self.engine.packet_dir(self.key) / 'verification.json').read_text())
        self.assertTrue(verification['passed'])
        outcomes = metrics(self.root / 'state/tasks.db')['quality_all_time']
        self.assertEqual(outcomes['submitted_unique_prs'], 1)
        self.assertEqual(outcomes['merged_prs'], 0)

    def test_batch_is_read_only_and_does_not_repeat_approved_candidate(self):
        packet, answer = self.packet()
        with self.research_mocks(answer, packet), patch.object(self.engine, 'advance') as advance:
            self.assertEqual(self.engine.research_batch(), [self.key])
            self.assertEqual(self.engine.research_batch(), [])
        advance.assert_not_called()
        self.assertEqual(self.created_issues, [])
        self.assertEqual(self.created_prs, [])
        self.assertEqual(self.p.task(self.key)['state'], 'DISCOVERED')

    def test_batch_limit_is_three_and_failed_research_has_bounded_retries(self):
        self.packet()
        for i in range(5):
            self.p.hunt('owner/repo', 'candidate ' + str(i))
        with patch.object(self.engine, 'research', side_effect=ValueError('bad evidence')) as research:
            self.assertEqual(len(self.engine.research_batch()), 3)
            self.assertEqual(research.call_count, 3)
            for _ in range(6):
                self.engine.research_batch()
        for row in self.p.db.execute('SELECT data FROM automation'):
            self.assertLessEqual(json.loads(row['data'])['attempts'].get('RESEARCH', 0), 3)
        self.assertEqual(self.created_issues, [])

    def test_auto_does_not_start_a_fourth_research_after_bounded_batch(self):
        self.packet()
        self.p.config['research'] = {'batch_on_auto': True, 'candidates_per_run': 3}
        for i in range(5):
            self.p.hunt('owner/repo', 'candidate ' + str(i))
        with patch.object(self.engine, 'research', return_value={}) as research, patch.object(self.engine, 'advance') as advance:
            self.engine.run()
        self.assertEqual(research.call_count, 3)
        advance.assert_not_called()

    def test_budget_pause_after_research_resumes_without_repeating_model(self):
        packet, answer = self.packet()
        with self.research_mocks(answer, packet), patch.object(self.engine, 'judge', side_effect=BudgetExhausted('run_model_calls_exhausted')):
            with self.assertRaises(BudgetExhausted):
                self.engine.advance(self.key)
        data = self.engine.load(self.key)
        self.assertIn('research_pending', data)
        self.assertEqual(data['stage'], 'PLAN')
        self.assertEqual(data['attempts'], {})
        with self.research_mocks(answer, packet), patch.object(self.engine, 'model') as model:
            self.assertEqual(self.engine.advance(self.key), 'WAITING_MAINTAINER')
        model.assert_not_called()
        self.assertEqual(self.created_prs, [])

    def test_policy_paused_matrix_never_creates_public_proposal(self):
        packet, answer = self.packet()
        answer['policy']['automated_pr'] = False
        with self.research_mocks(answer, packet):
            self.engine.advance(self.key)
        self.assertEqual(self.p.db.execute('SELECT queue FROM research_candidates').fetchone()[0], 'PAUSED_POLICY')
        self.assertEqual(self.created_issues, [])
        self.assertEqual(self.created_prs, [])

    def test_reviewer_model_is_configurable_and_identity_is_truthful(self):
        self.p.config['model'] = 'tokenking/devin/swe-2'
        events = json.dumps({'type': 'result', 'exit_code': 0, 'text': '{"approved":true,"reason":"fixture"}',
                             'tokens': {'input': 120, 'output': 10, 'cache_read': 0, 'cache_write': 0}})
        result = subprocess.CompletedProcess([], 0, events, '')
        with patch('autonomous.execute', return_value=result) as execute:
            first = self.engine.judge(self.key, 'fixture-review', {})
            self.p.config['review_model'] = 'configured/other'
            second = self.engine.judge(self.key, 'other-review', {})
        self.assertEqual(first['review_identity']['independence'], 'same_model_separate_channel')
        self.assertEqual(second['review_identity']['independence'], 'different_model_separate_channel')
        self.assertIn('configured', execute.call_args.args[0])
        self.assertEqual(metrics(self.root / 'state/tasks.db')['runs'][0]['usage']['input_tokens'], 120)

    def test_native_token_receipts_deduplicate_steps_but_zero_price_is_unknown(self):
        with run(self.p, 'usage') as current:
            current.call_id = current.reserve_call(self.key, 'test', 'model', 'opencode')
            event = json.dumps({'type': 'step_finish', 'part': {'id': 'step', 'cost': 0,
                               'tokens': {'input': 10, 'output': 5, 'cache': {'read': 20, 'write': 0}}}})
            record_usage(event + '\n' + event, 'opencode')
        usage = metrics(self.root / 'state/tasks.db')['runs'][0]['usage']
        self.assertEqual(usage['input_tokens'], 30)
        self.assertEqual(usage['output_tokens'], 5)
        self.assertIsNone(usage['actual_cost_usd'])

    def test_two_existing_repairs_in_one_run_only_implement_one_task(self):
        first = self.key
        second = self.p.hunt('owner/repo', 'second repair')
        for key in (first, second):
            self.engine.store(key, {'stage': 'REPAIR', 'attempts': {}, 'plan': self.plan})
        repaired = []
        def repair(key, data):
            repaired.append(key)
            self.engine.move(key, data, 'SKIPPED')
        with run(self.p, 'auto'), patch.object(self.engine, 'repair', side_effect=repair):
            self.engine.advance(first)
            self.engine.advance(second)
        self.assertEqual(repaired, [first])
        self.assertEqual(self.engine.load(second)['stage'], 'REPAIR')


del Fixture, ResearchFixture
