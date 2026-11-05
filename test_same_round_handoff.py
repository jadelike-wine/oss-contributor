import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from contribute import ContributionPaused
from telemetry import ACTIVE, BudgetExhausted, TaskBudgetExhausted, run
import test_conversion_runtime as fixtures
import test_research as research_fixtures


class SameRoundHandoffTests(unittest.TestCase):
    setUp = fixtures.RuntimeConversionTests.setUp
    tearDown = fixtures.RuntimeConversionTests.tearDown
    issue = fixtures.RuntimeConversionTests.issue

    def configure(self, slots=6):
        self.p.config['automation']['implementation_tasks_per_tick'] = slots
        self.p.config['research'].update(batch_on_auto=True, candidates_per_run=5)

    def approve(self, key, data, queue='READY_TO_IMPLEMENT'):
        data['research'] = {'approved': True, 'queue': queue,
                            'opportunity': {'category': 'bug', 'risk': 'low',
                                            'policy': {'automated_issue': True, 'automated_pr': True,
                                                       'maintainer_required': False}}}
        self.engine.store(key, data)
        # Research completion normally retains the task's DISCOVERED state and
        # automation PLAN stage; routing is held in the separate candidate row.
        with self.p.db:
            self.p.db.execute('INSERT OR REPLACE INTO research_candidates VALUES (?,?,?,?,?,?,?,?,?)',
                              ('feature-' + key, key, 'owner/repo', 'bug', queue, 70,
                               json.dumps({'legacy': True}), '2026-11-05', '2026-11-05'))

    def test_fresh_ready_candidate_enters_normal_delivery_in_same_run(self):
        self.configure()
        key = self.p.hunt('owner/repo', 'fresh candidate')
        seen = []
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research', side_effect=self.approve), \
                patch.object(self.engine, 'plan', side_effect=lambda task, data: seen.append(task)), \
                run(self.p, 'handoff') as current:
            self.engine.run()
        self.assertEqual([key], seen)
        self.assertEqual({key}, current.task_budget.used['research'])
        self.assertEqual({key}, current.task_budget.used['implementation'])
        self.assertEqual('DISCOVERED', self.p.task(key)['state'])

    def test_unconfirmed_and_policy_paused_research_are_not_handed_off(self):
        self.configure()
        queues = {self.p.hunt('owner/repo', name): queue for name, queue in
                  [('ready', 'READY_TO_IMPLEMENT'), ('proposal', 'NEEDS_MAINTAINER_CONFIRMATION'),
                   ('policy', 'PAUSED_POLICY'), ('unsupported', 'RESEARCH_ONLY')]}
        seen = []
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research', side_effect=lambda key, data: self.approve(key, data, queues[key])), \
                patch.object(self.engine, 'plan', side_effect=lambda key, data: seen.append(key)):
            self.engine.run()
        self.assertEqual([key for key, queue in queues.items() if queue == 'READY_TO_IMPLEMENT'], seen)

    def test_existing_dispatch_is_not_repeated_by_handoff(self):
        self.configure()
        existing = self.issue(1)
        fresh = self.p.hunt('owner/repo', 'fresh candidate')
        seen = []
        def completed_batch(locked=False, preferred_task_ids=()):
            self.approve(fresh, self.engine.load(fresh))
            self.approve(existing, self.engine.load(existing))
            return [existing, fresh, fresh]
        with patch.object(self.engine, 'research_batch', side_effect=completed_batch), \
                patch.object(self.engine, 'plan', side_effect=lambda key, data: seen.append(key)):
            self.engine.run()
        self.assertEqual([existing, fresh], seen)

    def test_exhausted_implementation_slots_leave_ready_research_for_next_run(self):
        self.configure(slots=1)
        existing = self.issue(1)
        fresh = self.p.hunt('owner/repo', 'fresh candidate')
        seen = []
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research', side_effect=self.approve), \
                patch.object(self.engine, 'plan', side_effect=lambda key, data: seen.append(key)), \
                run(self.p, 'capacity') as current:
            self.engine.run()
        self.assertEqual([existing], seen)
        self.assertTrue(self.engine.load(fresh)['research']['approved'])
        self.assertEqual('PLAN', self.engine.load(fresh)['stage'])
        self.assertEqual({existing}, current.task_budget.used['implementation'])

    def test_policy_pause_does_not_consume_the_only_implementation_slot(self):
        self.configure(slots=1)
        blocked = self.issue(1)
        fresh = self.p.hunt('owner/repo', 'fresh candidate')
        seen = []
        def policy(task, **kwargs):
            if task['id'] == blocked:
                raise ContributionPaused('Upstream requires issue labels: ready')
        self.p.require_contribution = policy
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research', side_effect=self.approve), \
                patch.object(self.engine, 'plan', side_effect=lambda key, data: seen.append(key)), \
                run(self.p, 'policy') as current:
            self.engine.run()
        self.assertEqual([fresh], seen)
        self.assertEqual({fresh}, current.task_budget.used['implementation'])
        self.assertEqual('PLAN', self.engine.load(blocked)['stage'])
        self.assertEqual({}, self.engine.load(blocked)['attempts'])

    def test_task_budget_yield_during_handoff_allows_another_ready_candidate(self):
        self.configure()
        keys = [self.p.hunt('owner/repo', 'fresh ' + str(i)) for i in range(2)]
        seen = []
        def plan(key, data):
            seen.append(key)
            if len(seen) == 1:
                raise TaskBudgetExhausted('task_model_calls_exhausted')
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research', side_effect=self.approve), \
                patch.object(self.engine, 'plan', side_effect=plan), run(self.p, 'local-yield') as current:
            self.engine.run()
        self.assertEqual(set(keys), set(seen))
        self.assertEqual(2, len(seen))
        self.assertEqual('task_model_calls_exhausted', self.engine.load(seen[0])['stop_reason'])
        self.assertIsNone(current.reason)

    def test_open_service_circuit_does_not_consume_local_stage_capacity(self):
        self.configure(slots=1)
        blocked = self.issue(1, 'PLAN')
        local = self.issue(2, 'REPRODUCE')
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'plan') as plan, \
                patch.object(self.engine, 'reproduce') as reproduce, run(self.p, 'circuit-slots') as current:
            current.channel_failures['opencode'] = 2
            self.engine.run()
        plan.assert_not_called()
        reproduce.assert_called_once()
        self.assertEqual({local}, current.task_budget.used['implementation'])
        self.assertEqual('PLAN', self.engine.load(blocked)['stage'])

    def test_research_task_budget_yield_does_not_abort_batch_or_handoff(self):
        self.configure()
        keys = [self.p.hunt('owner/repo', 'fresh ' + str(i)) for i in range(2)]
        researched, planned = [], []
        def research(key, data):
            researched.append(key)
            if len(researched) == 1:
                raise TaskBudgetExhausted('task_time_budget_exhausted')
            self.approve(key, data)
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research', side_effect=research), \
                patch.object(self.engine, 'plan', side_effect=lambda key, data: planned.append(key)):
            self.engine.run()
        self.assertEqual(set(keys), set(researched))
        self.assertEqual([researched[1]], planned)
        self.assertEqual('task_time_budget_exhausted', self.engine.load(researched[0])['stop_reason'])

    def test_ready_checkpoint_before_research_budget_yield_is_not_retried_this_run(self):
        self.configure()
        key = self.p.hunt('owner/repo', 'fresh candidate')
        def research(task, data):
            self.approve(task, data)
            raise TaskBudgetExhausted('task_time_budget_exhausted')
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research', side_effect=research), \
                patch.object(self.engine, 'plan') as plan, run(self.p, 'checkpoint-yield') as current:
            self.engine.run()
        plan.assert_not_called()
        self.assertTrue(self.engine.load(key)['research']['approved'])
        self.assertIn(key, current.paused_tasks)
        self.assertEqual(set(), current.task_budget.used['implementation'])

    def test_source_scope_priority_shares_the_total_research_quota(self):
        self.configure()
        self.p.config['research'].update(candidates_per_run=3, source_discovery_per_run=2)
        regular = [self.p.hunt('owner/repo', 'regular ' + str(i)) for i in range(4)]
        source, seen = [], []
        def register(engine, limit):
            self.assertIs(engine, self.engine)
            self.assertEqual(2, limit)
            source.extend(self.p.hunt('owner/repo', 'source scope ' + str(i)) for i in range(limit))
            with self.p.db:
                self.p.db.executemany('UPDATE tasks SET score=0 WHERE id=?', [(key,) for key in source])
            return source
        module = SimpleNamespace(register_scopes=register)
        with patch.dict(sys.modules, {'source_discovery': module}), \
                patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research', side_effect=lambda key, data: seen.append(key)), \
                patch.object(self.engine, 'advance') as advance, run(self.p, 'source-quota') as current:
            self.engine.run()
        self.assertEqual(set(source), set(seen[:2]))
        self.assertIn(seen[2], regular)
        self.assertEqual(3, len(seen))
        self.assertEqual(set(seen), current.task_budget.used['research'])
        advance.assert_not_called()

    def test_standalone_batch_does_not_register_source_scopes(self):
        self.configure()
        self.p.config['research']['source_discovery_per_run'] = 2
        key = self.p.hunt('owner/repo', 'regular candidate')
        def forbidden(*args):
            self.fail('Standalone research-batch must not register source scopes')
        with patch.dict(sys.modules, {'source_discovery': SimpleNamespace(register_scopes=forbidden)}), \
                patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research'), patch.object(self.engine, 'advance') as advance:
            self.assertEqual([key], self.engine.research_batch())
        advance.assert_not_called()

    def test_backlogged_source_scopes_stay_within_two_and_regular_work_fills_batch(self):
        self.configure()
        self.p.config['research'].update(candidates_per_run=8, source_discovery_per_run=2)
        source = [self.p.hunt('owner/repo', 'queued source ' + str(i)) for i in range(3)]
        regular = [self.p.hunt('owner/repo', 'regular ' + str(i)) for i in range(8)]
        with self.p.db:
            for key in source:
                self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',
                                  (json.dumps({'source_discovery': {'scope_id': key}}), key))
        seen = []
        module = SimpleNamespace(register_scopes=lambda engine, limit: source[:2])
        with patch.dict(sys.modules, {'source_discovery': module}), \
                patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research', side_effect=lambda key, data: seen.append(key)), \
                run(self.p, 'source-backlog') as current:
            self.engine.run()
        self.assertEqual(set(source[:2]), set(seen) & set(source))
        self.assertEqual(6, len(set(seen) & set(regular)))
        self.assertEqual(8, len(seen))
        self.assertEqual(set(seen), current.task_budget.used['research'])

    def test_disabled_source_lane_skips_existing_scopes_in_standalone_batch(self):
        self.configure()
        source = self.p.hunt('owner/repo', 'queued source')
        regular = self.p.hunt('owner/repo', 'regular candidate')
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',
                              (json.dumps({'source_discovery': {'scope_id': source}}), source))
        for configured in (None, 0):
            if configured is None:
                self.p.config['research'].pop('source_discovery_per_run', None)
            else:
                self.p.config['research']['source_discovery_per_run'] = configured
            with self.subTest(source_limit=configured), \
                    patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                    patch.object(self.engine, 'research') as research:
                self.assertEqual([regular], self.engine.research_batch())
            self.assertEqual(regular, research.call_args.args[0])

    def test_handoff_keeps_global_model_call_cap(self):
        self.configure()
        key = self.p.hunt('owner/repo', 'fresh candidate')
        self.p.config['budgets']['run_model_calls'] = 1
        def research(task, data):
            ACTIVE.get().reserve_call(task, 'research', 'model', 'hermes')
            self.approve(task, data)
        def plan(task, data):
            ACTIVE.get().reserve_call(task, 'plan', 'model', 'opencode')
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research', side_effect=research), \
                patch.object(self.engine, 'plan', side_effect=plan):
            with self.assertRaisesRegex(BudgetExhausted, 'run_model_calls_exhausted'):
                self.engine.run()
        self.assertEqual(1, self.p.db.execute('SELECT count(*) FROM model_calls').fetchone()[0])
        self.assertTrue(self.engine.load(key)['research']['approved'])


class SameRoundPipelineTests(unittest.TestCase):
    # Real local Git/red-green verification; GitHub and model adapters are fake.
    setUp = research_fixtures.ResearchTests.setUp
    tearDown = research_fixtures.ResearchTests.tearDown
    git = research_fixtures.ResearchTests.git
    model = research_fixtures.ResearchTests.model
    mocks = research_fixtures.ResearchTests.mocks
    fake_gh = research_fixtures.ResearchTests.fake_gh
    fake_command = research_fixtures.ResearchTests.fake_command
    packet = research_fixtures.ResearchTests.packet
    research_mocks = research_fixtures.ResearchTests.research_mocks

    def test_one_auto_run_researches_verifies_and_prepares_mock_publication(self):
        packet = self.packet()
        self.p.config['research'] = {'batch_on_auto': True, 'candidates_per_run': 1}
        with self.research_mocks(research_fixtures.opportunity(), packet):
            self.engine.run()
        self.assertEqual('PR_OPEN', self.engine.load(self.key)['stage'])
        self.assertEqual(1, len(self.created_issues))
        self.assertEqual(1, len(self.created_prs))
        verification = json.loads((self.engine.packet_dir(self.key) / 'verification.json').read_text())
        self.assertTrue(verification['passed'])


if __name__ == '__main__':
    unittest.main()
