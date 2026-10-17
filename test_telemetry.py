import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from contribute import Pipeline, execute
from telemetry import ACTIVE, BudgetExhausted, metrics, migrate, run, stage


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config.json').write_text(json.dumps({'repositories': ['owner/repo'], 'timeout_seconds': 30}))
        self.p = Pipeline(self.root)
        self.addCleanup(self.p.db.close)

    def test_unique_runs_stage_monotonic_durations_and_null_cost(self):
        ids = []
        for _ in range(2):
            with run(self.p, 'test') as current, stage('research', 'task'):
                ids.append(current.id)
                current.reserve_call('task', 'research', 'test/model', 'mock')
                time.sleep(.03)  # Windows monotonic resolution can exceed two milliseconds.
        report = metrics(self.root / 'state/tasks.db')
        self.assertNotEqual(*ids)
        self.assertEqual(len(report['runs']), 2)
        self.assertGreater(report['runs'][0]['stages'][0]['elapsed'], 0)
        self.assertIsNone(report['runs'][0]['usage']['actual_cost_usd'])
        self.assertFalse(report['cost_caps_enforced'])

    def test_budget_reserved_before_call_and_survives_next_run(self):
        self.p.config['budgets'] = {'run_model_calls': 1, 'task_model_calls': 1}
        with self.assertRaises(BudgetExhausted):
            with run(self.p, 'test') as current:
                current.reserve_call('task', 'first', 'test/model', 'mock')
                current.reserve_call('task', 'second', 'test/model', 'mock')
        with self.assertRaisesRegex(BudgetExhausted, 'task_model_calls'):
            with run(self.p, 'resume') as current:
                current.reserve_call('task', 'third', 'test/model', 'mock')
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM model_calls').fetchone()[0], 1)

    def test_daily_call_and_runtime_budget(self):
        self.p.config['budgets'] = {'daily_model_calls': 1}
        with run(self.p, 'first') as current:
            current.reserve_call('one', 'research', 'model', 'mock')
        with self.assertRaisesRegex(BudgetExhausted, 'daily_model_calls'):
            with run(self.p, 'second') as current:
                current.reserve_call('two', 'research', 'model', 'mock')
        self.p.config['budgets'] = {'daily_seconds': 0}
        with self.assertRaisesRegex(BudgetExhausted, 'time_budget'):
            with run(self.p, 'third') as current:
                current.remaining()

    def test_command_timeout_is_capped_by_run_and_partial_state_retained(self):
        self.p.config['budgets'] = {'run_seconds': .1}
        task = self.p.hunt('owner/repo', 'preserve me')
        with self.assertRaises(BudgetExhausted):
            with run(self.p, 'test'), stage('testing', task):
                execute([sys.executable, '-c', 'import time; time.sleep(5)'], timeout=30)
        self.assertEqual(self.p.task(task)['state'], 'DISCOVERED')
        report = metrics(self.root / 'state/tasks.db')
        self.assertEqual(report['runs'][0]['outcome'], 'budget_exhausted')
        self.assertEqual(report['runs'][0]['counters']['timeouts'], 1)

    def test_exceptions_pauses_and_command_failures_have_distinct_outcomes(self):
        from contribute import ContributionPaused
        for error, outcome in [(ValueError('secret must not be logged'), 'failed'),
                               (ContributionPaused('policy'), 'paused_policy'),
                               (subprocess.TimeoutExpired('hidden argv', 1), 'timeout')]:
            with self.assertRaises(type(error)):
                with run(self.p, 'test'), stage('research'):
                    raise error
            record = metrics(self.root / 'state/tasks.db')['runs'][0]
            self.assertEqual(record['outcome'], outcome)
            self.assertNotIn('secret must not', json.dumps(record))
        with run(self.p, 'failure'):
            execute([sys.executable, '-c', 'raise SystemExit(3)'])
        self.assertEqual(metrics(self.root / 'state/tasks.db')['runs'][0]['counters']['command_failures'], 1)

    def test_read_only_metrics_does_not_change_database_or_call_remote(self):
        with run(self.p, 'once'), stage('research'):
            pass
        before = list(self.p.db.iterdump())
        with patch('contribute.gh') as remote, patch('autonomous.Autonomous.model') as model:
            metrics(self.root / 'state/tasks.db')
            metrics(self.root / 'state/tasks.db')
        remote.assert_not_called()
        model.assert_not_called()
        self.assertEqual(before, list(self.p.db.iterdump()))

    def test_migration_is_repeatable_and_preserves_legacy_rows(self):
        key = self.p.hunt('owner/repo', 'legacy')
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET state='WAITING_MAINTAINER',base_sha='old' WHERE id=?", (key,))
        before = self.p.task(key)
        for _ in range(3):
            migrate(self.p.db)
        self.assertEqual(before, self.p.task(key))
        self.assertEqual(self.p.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_old_database_metrics_does_not_migrate(self):
        path = self.root / 'legacy.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE tasks(id TEXT)')
        db.close()
        before = path.read_bytes()
        self.assertEqual(metrics(path)['runs'], [])
        self.assertEqual(path.read_bytes(), before)

    def test_interrupted_stage_not_counted_as_success_or_duplicated(self):
        with self.assertRaises(KeyboardInterrupt):
            with run(self.p, 'test'), stage('research'):
                raise KeyboardInterrupt()
        first = metrics(self.root / 'state/tasks.db')
        second = metrics(self.root / 'state/tasks.db')
        self.assertEqual(first, second)
        self.assertEqual(first['runs'][0]['stages'][0]['outcome'], 'interrupted')

    def test_task_budget_does_not_charge_other_candidates_time(self):
        self.p.config['budgets'] = {'task_seconds': 10}
        clock = [100.0]
        with patch('telemetry.time.monotonic', side_effect=lambda: clock[0]):
            with run(self.p, 'batch') as current:
                with stage('research', 'one'):
                    clock[0] += 1
                with stage('research', 'two'):
                    clock[0] += 9
                with stage('implementation', 'one'):
                    clock[0] += 1
                    self.assertGreater(current.remaining(), 7)


if __name__ == '__main__':
    unittest.main()
