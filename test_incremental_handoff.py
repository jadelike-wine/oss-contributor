"""Auto must use ready work before later research consumes the shared budget."""
import json
import unittest
from unittest.mock import patch

from telemetry import ACTIVE, BudgetExhausted, TaskBudgetExhausted, run, stage
import test_same_round_handoff as fixtures


class IncrementalHandoffTests(unittest.TestCase):
    setUp = fixtures.SameRoundHandoffTests.setUp
    tearDown = fixtures.SameRoundHandoffTests.tearDown
    issue = fixtures.SameRoundHandoffTests.issue
    configure = fixtures.SameRoundHandoffTests.configure
    approve = fixtures.SameRoundHandoffTests.approve

    def test_ready_candidate_is_delivered_before_next_research_uses_budget(self):
        self.configure()
        keys = [self.p.hunt('owner/repo', 'candidate ' + str(i)) for i in range(2)]
        self.p.config['budgets']['run_model_calls'] = 4
        events = []

        def research(key, data):
            ACTIVE.get().reserve_call(key, 'research', 'model', 'hermes')
            events.append(('research', key))
            self.approve(key, data)

        def plan(key, data):
            ACTIVE.get().reserve_call(key, 'plan', 'model', 'opencode')
            events.append(('delivery', key))

        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research', side_effect=research), \
                patch.object(self.engine, 'plan', side_effect=plan):
            self.engine.run()
        first, second = [key for kind, key in events if kind == 'research']
        self.assertEqual(set(keys), {first, second})
        self.assertEqual([('research', first), ('delivery', first),
                          ('research', second), ('delivery', second)], events)
        self.assertEqual(4, self.p.db.execute('SELECT count(*) FROM model_calls').fetchone()[0])

    def test_later_research_cannot_spend_a_ready_candidates_delivery_opportunity(self):
        self.configure()
        self.p.config['budgets']['run_model_calls'] = 2
        for i in range(2):
            self.p.hunt('owner/repo', 'candidate ' + str(i))
        events = []

        def research(key, data):
            ACTIVE.get().reserve_call(key, 'research', 'model', 'hermes')
            events.append(('research', key))
            self.approve(key, data)

        def plan(key, data):
            ACTIVE.get().reserve_call(key, 'plan', 'model', 'opencode')
            events.append(('delivery', key))

        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research', side_effect=research), \
                patch.object(self.engine, 'plan', side_effect=plan):
            with self.assertRaisesRegex(BudgetExhausted, 'run_model_calls_exhausted'):
                self.engine.run()
        self.assertEqual(['research', 'delivery'], [kind for kind, _ in events])
        self.assertEqual(events[0][1], events[1][1])
        self.assertEqual(2, self.p.db.execute('SELECT count(*) FROM model_calls').fetchone()[0])
        self.assertIsNone(getattr(self.engine, '_research_handoff', None))

    def test_standalone_batch_stays_read_only_after_an_auto_run(self):
        self.configure()
        first = self.p.hunt('owner/repo', 'auto candidate')
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research', side_effect=self.approve), \
                patch.object(self.engine, 'plan') as plan:
            self.engine.run()
            plan.assert_called_once()
            self.assertEqual(first, plan.call_args.args[0])
            plan.reset_mock()
            second = self.p.hunt('owner/repo', 'standalone candidate')
            self.assertEqual([second], self.engine.research_batch())
            plan.assert_not_called()

    def test_standalone_batch_stays_read_only_after_handoff_exception(self):
        self.configure()
        self.p.hunt('owner/repo', 'auto candidate')
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research', side_effect=self.approve), \
                patch.object(self.engine, 'handoff_research', side_effect=BudgetExhausted('test cap')):
            with self.assertRaisesRegex(BudgetExhausted, 'test cap'):
                self.engine.run()
        self.assertIsNone(getattr(self.engine, '_research_handoff', None))
        second = self.p.hunt('owner/repo', 'standalone candidate')
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research', side_effect=self.approve), \
                patch.object(self.engine, 'advance') as advance:
            self.assertEqual([second], self.engine.research_batch())
            advance.assert_not_called()

    def test_empty_research_releases_share_for_yielded_and_unvisited_delivery(self):
        self.configure()
        keys = [self.issue(i) for i in range(1, 3)]
        self.p.config['budgets']['run_model_calls'] = 2
        self.p.config['research']['reserve_model_calls'] = 2
        delivered = []

        def plan(key, data):
            ACTIVE.get().reserve_call(key, 'plan', 'model', 'opencode')
            delivered.append(key)

        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'plan', side_effect=plan), \
                patch.object(self.engine, 'research') as research, run(self.p, 'empty-share') as current:
            self.engine.run()
            self.assertFalse(current.reserve_research)
            self.assertEqual('success', current.outcome)
            self.assertIsNone(current.reason)
        self.assertEqual(keys, delivered)
        research.assert_not_called()
        self.assertEqual(set(keys), current.task_budget.used['implementation'])
        self.assertEqual(2, self.p.db.execute('SELECT count(*) FROM model_calls').fetchone()[0])
        saved_run = self.p.db.execute('SELECT outcome,stop_reason FROM runs WHERE run_id=?', (current.id,)).fetchone()
        self.assertEqual(('success', None), tuple(saved_run))

    def test_empty_research_reclaims_reserved_time_without_extending_deadline(self):
        self.configure()
        key = self.issue(1)
        self.p.config['budgets'].update(run_seconds=100, daily_seconds=1000)
        self.p.config['research']['reserve_seconds'] = 20
        remaining = []
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'plan',
                             side_effect=lambda task, data: remaining.append(ACTIVE.get().remaining())), \
                run(self.p, 'reserved-time') as current:
            with patch('telemetry.time.monotonic', return_value=current.start + 85):
                self.engine.run()
        self.assertEqual([15], remaining)
        self.assertEqual({key}, current.task_budget.used['implementation'])

    def test_unusable_research_releases_share_without_resetting_retries(self):
        for unavailable in ('exhausted', 'cooldown', 'circuit', 'source_disabled'):
            with self.subTest(reason=unavailable):
                self.configure()
                self.p.config['research'].update(model_channel='hermes', reserve_model_calls=1)
                self.p.config['budgets']['run_model_calls'] = 1
                candidate = self.p.hunt('owner/repo', 'unavailable ' + unavailable)
                saved = self.engine.load(candidate)
                if unavailable == 'exhausted':
                    saved['attempts']['RESEARCH'] = 3
                elif unavailable == 'cooldown':
                    saved['infrastructure_failures'] = {'RESEARCH': {'retry_after': '2099-01-01T00:00:00+00:00'}}
                elif unavailable == 'source_disabled':
                    with self.p.db:
                        self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',
                                          (json.dumps({'source_discovery': {'scope_id': candidate}}), candidate))
                self.engine.store(candidate, saved)
                key = self.issue(100 + len(unavailable))
                delivered = []

                def plan(task, data):
                    ACTIVE.get().reserve_call(task, 'plan', 'model', 'opencode')
                    delivered.append(task)
                    self.engine.move(task, data, 'SKIPPED')

                with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                        patch.object(self.engine, 'plan', side_effect=plan), \
                        patch.object(self.engine, 'research') as research, run(self.p, unavailable) as current:
                    if unavailable == 'circuit':
                        current.channel_failures['hermes'] = 2
                    self.engine.run()
                self.assertEqual([key], delivered)
                research.assert_not_called()
                self.assertEqual(saved, self.engine.load(candidate))
                with self.p.db:
                    self.p.db.execute("UPDATE tasks SET state='SKIPPED' WHERE id=?", (candidate,))

    def test_share_yield_resumes_saved_stage_without_repeating_completed_plan(self):
        self.configure()
        key = self.issue(1)
        self.p.config['budgets']['run_model_calls'] = 2
        self.p.config['research']['reserve_model_calls'] = 1
        completed = []

        def plan(task, data):
            ACTIVE.get().reserve_call(task, 'plan', 'model', 'opencode')
            completed.append('plan')
            self.engine.move(task, data, 'REPRODUCE')

        def reproduce(task, data):
            ACTIVE.get().reserve_call(task, 'reproduce', 'model', 'hermes')
            completed.append('reproduce')

        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'plan', side_effect=plan), \
                patch.object(self.engine, 'reproduce', side_effect=reproduce), run(self.p, 'resume') as current:
            self.engine.run()
        self.assertEqual(['plan', 'reproduce'], completed)
        self.assertEqual({key}, current.task_budget.used['implementation'])
        self.assertEqual({}, self.engine.load(key)['attempts'])
        self.assertIsNone(self.engine.load(key).get('stop_reason'))

    def test_resumed_delivery_does_not_erase_an_earlier_real_run_failure(self):
        self.configure()
        key = self.issue(1)
        self.p.config['budgets']['run_model_calls'] = 1
        self.p.config['research']['reserve_model_calls'] = 1

        def plan(task, data):
            ACTIVE.get().reserve_call(task, 'plan', 'model', 'opencode')

        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'plan', side_effect=plan), run(self.p, 'prior-failure') as current:
            try:
                with stage('earlier-network'):
                    raise ConnectionError('connection reset')
            except ConnectionError:
                pass
            self.assertEqual(('network', 'ConnectionError'), (current.outcome, current.reason))
            self.engine.run()
            self.assertEqual(('network', 'ConnectionError'), (current.outcome, current.reason))
        self.assertIsNone(self.engine.load(key).get('stop_reason'))

    def test_task_budget_pause_is_not_replayed_when_research_finishes(self):
        self.configure()
        key = self.issue(1)
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'plan', side_effect=TaskBudgetExhausted('task limit')) as plan, \
                run(self.p, 'paused') as current:
            self.engine.run()
        plan.assert_called_once()
        self.assertIn(key, current.paused_tasks)

    def test_new_unconfirmed_research_is_not_dispatched_by_resumed_delivery(self):
        self.configure()
        self.p.hunt('owner/repo', 'unconfirmed candidate')
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), \
                patch.object(self.engine, 'research',
                             side_effect=lambda key, data: self.approve(key, data, 'NEEDS_MAINTAINER_CONFIRMATION')), \
                patch.object(self.engine, 'advance') as advance:
            self.engine.run()
        advance.assert_not_called()

    def test_incremental_delivery_preserves_research_rotation_across_runs(self):
        self.configure()
        repos = ['owner/repo', 'other/repo']
        self.p.config['repositories'] = repos
        self.p.config['research']['candidates_per_run'] = 1
        for repo in repos:
            self.p.hunt(repo, 'candidate')
        researched = []

        def research(key, data):
            researched.append(self.p.task(key)['repo'])
            self.approve(key, data)

        with patch('repository_catalog.research_pool', return_value=repos), \
                patch.object(self.engine, 'research', side_effect=research), \
                patch.object(self.engine, 'plan'):
            self.engine.run()
            self.engine.run()
        self.assertEqual(repos, researched)
        self.assertEqual(repos[-1], self.p.db.execute(
            "SELECT last_repo FROM scheduler WHERE name='research'").fetchone()[0])


if __name__ == '__main__':
    unittest.main()
