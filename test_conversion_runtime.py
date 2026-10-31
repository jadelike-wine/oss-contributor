import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autonomous import Autonomous
from contribute import ContributionPaused, Pipeline
from failures import record_failure, retry_ready
import telemetry


class RuntimeConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = {'repositories': ['owner/repo'], 'automation': {'enabled': True, 'max_tasks_per_tick': 6},
                  'research': {'candidates_per_run': 1}, 'budgets': {'run_model_calls': 16}}
        (self.root / 'config.json').write_text(json.dumps(config), encoding='utf-8')
        self.p = Pipeline(self.root)
        self.engine = Autonomous(self.p)
        self.p.effective_repositories = lambda contribution=False: self.p.config['repositories'][:]
        self.p.require_contribution = lambda *args, **kwargs: None
        self.p.report = lambda: None

    def tearDown(self):
        self.p.db.close()
        self.temp.cleanup()

    def issue(self, number, stage='PLAN'):
        key = 'owner--repo--' + str(number)
        with self.p.db:
            self.p.db.execute('''INSERT INTO tasks(id,repo,number,source,title,state,holds,payload,updated)
                                 VALUES (?,'owner/repo',?,'issue','Regression',?,'[]','{}','2026-10-31')''',
                              (key, number, stage))
        self.engine.store(key, {'stage': stage, 'attempts': {}})
        return key

    def test_telemetry_reports_failure_domain_instead_of_generic_quality_failure(self):
        cases = [(ConnectionError('connection reset'), 'network'),
                 (RuntimeError('HTTP 429 rate limit'), 'rate_limit'),
                 (RuntimeError('TLS certificate verify failed'), 'certificate'),
                 (RuntimeError('HTTP 503 service unavailable'), 'service_unavailable'),
                 (json.JSONDecodeError('malformed response', '{', 1), 'model_format'),
                 (ValueError('quote does not match pinned evidence'), 'insufficient_evidence'),
                 (RuntimeError('regression assertion failed'), 'patch_error'),
                 (ContributionPaused('ready label required'), 'paused_policy')]
        for error, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, telemetry.classify(error))

    def test_network_and_json_failures_preserve_quality_count_and_cool_down(self):
        for number, error in [(1, ConnectionError('connection reset')), (2, json.JSONDecodeError('bad response', '', 0))]:
            task = self.issue(number)
            self.engine.store(task, {'stage': 'PLAN', 'attempts': {'PLAN': 2}})
            with self.subTest(error=type(error).__name__), patch.object(self.engine, 'plan', side_effect=error) as invoke:
                self.assertEqual('PLAN', self.engine.advance(task))
                data = self.engine.load(task)
                self.assertEqual(2, data['attempts']['PLAN'])
                self.assertEqual(1, data['infrastructure_failures']['PLAN']['count'])
                self.engine.advance(task)
                self.assertEqual(1, invoke.call_count)

    def test_cooldown_caps_at_one_hour_and_exhaustion_does_not_expire(self):
        data = {'attempts': {'PLAN': 2}}
        at = dt.datetime(2026, 10, 31, tzinfo=dt.timezone.utc)
        options = {'max_infrastructure_attempts': 3, 'infrastructure_cooldown_seconds': 2000}
        for attempt in range(3):
            record_failure(data, 'PLAN', RuntimeError('HTTP 503'), options, at)
            saved = data['infrastructure_failures']['PLAN']
            delay = (dt.datetime.fromisoformat(saved['retry_after']) - at).total_seconds()
            self.assertLessEqual(delay, 3600)
            self.assertEqual(2, data['attempts']['PLAN'])
        self.assertFalse(retry_ready(data, 'PLAN', at + dt.timedelta(days=30)))
        self.assertEqual('infrastructure_retry_exhausted', data['stop_reason'])

    def test_service_circuit_stops_third_model_but_allows_local_stage(self):
        tasks = [self.issue(number) for number in range(1, 4)]
        local = self.issue(4, 'REPRODUCE')
        calls, local_calls = [], []

        @telemetry.model_call('opencode')
        def unavailable(engine, task, purpose):
            calls.append(task)
            raise RuntimeError('HTTP 503 service unavailable')

        with telemetry.run(self.p, 'service-circuit'), \
                patch.object(self.engine, 'plan', side_effect=lambda key, data: unavailable(self.engine, key, 'plan')), \
                patch.object(self.engine, 'reproduce', side_effect=lambda key, data: local_calls.append(key)):
            for task in tasks:
                self.engine.advance(task)
            self.engine.advance(local)
        self.assertEqual(tasks[:2], calls)
        self.assertEqual([local], local_calls)
        self.assertEqual(2, self.p.db.execute('SELECT count(*) FROM model_calls').fetchone()[0])

    def test_reserved_research_calls_keep_total_budget_unchanged(self):
        self.p.config['budgets']['run_model_calls'] = 4
        self.p.config['research']['reserve_model_calls'] = 2
        share_error = getattr(telemetry, 'DeliveryBudgetExhausted', telemetry.BudgetExhausted)
        with telemetry.run(self.p, 'research-share') as current:
            current.reserve_research = True
            for number in range(2):
                current.reserve_call('task', 'plan', 'model', 'opencode')
            with self.assertRaisesRegex(share_error, 'delivery_model_share'):
                current.reserve_call('task', 'plan', 'model', 'opencode')
            current.reserve_research = False
            for number in range(2):
                current.reserve_call('research', 'research', 'model', 'hermes')
            with self.assertRaisesRegex(telemetry.BudgetExhausted, 'run_model_calls'):
                current.reserve_call('research', 'research', 'model', 'hermes')
        self.assertEqual(4, self.p.db.execute('SELECT count(*) FROM model_calls').fetchone()[0])

    def test_reserved_research_time_is_a_share_not_an_extended_run(self):
        self.p.config['budgets'].update(run_seconds=100, daily_seconds=1000)
        self.p.config['research']['reserve_seconds'] = 20
        share_error = getattr(telemetry, 'DeliveryBudgetExhausted', telemetry.BudgetExhausted)
        with telemetry.run(self.p, 'time-share') as current:
            with patch('telemetry.time.monotonic', return_value=current.start + 80.5):
                current.reserve_research = True
                with self.assertRaisesRegex(share_error, 'delivery_time_share'):
                    current.remaining()
                current.reserve_research = False
                self.assertAlmostEqual(19.5, current.remaining())

    def test_research_only_repository_gets_persistent_round_robin_turns(self):
        first = self.p.hunt('owner/repo', 'cancel operation')
        second = self.p._register_hunt('read/only', 'cancel operation')
        seen = []
        with patch('repository_catalog.research_pool', return_value=['owner/repo', 'read/only']), \
                patch.object(self.engine, 'research', side_effect=lambda key, data: seen.append(key)):
            for _ in range(3):
                self.engine.research_batch()
        self.assertEqual([first, second, first], seen)

    def test_recovery_requires_dead_owner_dead_child_and_explicit_legacy_audit(self):
        recover = getattr(telemetry, 'recover_interrupted', None)
        self.assertTrue(callable(recover), 'Recovery must inspect recorded owner/child PIDs before closing telemetry')
        with self.p.db:
            self.p.db.executemany('INSERT INTO runs(run_id,entry,started,outcome,owner_pid) VALUES (?,?,?,?,?)',
                                 [('live', 'auto', '2026-10-30', 'running', 100),
                                  ('dead', 'auto', '2026-10-30', 'running', 200),
                                  ('legacy', 'auto', '2026-10-30', 'running', None)])
            self.p.db.execute("INSERT INTO locks(name,pid,child_pid,started) VALUES ('worker',300,400,'2026-10-30')")
        alive = {100, 400}
        self.assertEqual([], recover(self.p.db, lambda pid: pid in alive, legacy_processes_checked=True))
        alive.remove(400)
        self.assertEqual(['dead'], recover(self.p.db, lambda pid: pid in alive))
        self.assertIsNone(self.p.db.execute("SELECT ended FROM runs WHERE run_id='legacy'").fetchone()[0])
        self.assertIsNone(self.p.db.execute("SELECT ended FROM runs WHERE run_id='live'").fetchone()[0])
        self.assertEqual(['legacy'], recover(self.p.db, lambda pid: pid in alive, legacy_processes_checked=True))
        self.assertEqual([], recover(self.p.db, lambda pid: pid in alive, legacy_processes_checked=True))
        reason = self.p.db.execute("SELECT stop_reason FROM runs WHERE run_id='dead'").fetchone()[0]
        self.assertIn('resume_from_saved_task_stage', reason)
        self.assertEqual('ok', self.p.db.execute('PRAGMA integrity_check').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
