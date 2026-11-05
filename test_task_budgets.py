import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from contribute import Pipeline, execute
from telemetry import BudgetExhausted, TaskBudgetExhausted, run, stage


class ScopedTaskBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config.json').write_text(json.dumps({'repositories': []}), encoding='utf-8')
        self.p = Pipeline(self.root)
        self.addCleanup(self.p.db.close)

    def test_task_time_yields_without_poisoning_other_tasks_or_run(self):
        self.p.config['budgets'] = {'task_seconds': 10, 'run_seconds': 100}
        clock = [100.0]
        with patch('telemetry.time.monotonic', side_effect=lambda: clock[0]):
            with run(self.p, 'scoped') as current:
                with self.assertRaisesRegex(TaskBudgetExhausted, 'task_time'):
                    with stage('planning', 'slow'):
                        clock[0] += 11
                with stage('planning', 'other'):
                    self.assertEqual(10, current.remaining())
                self.assertIsNone(current.reason)
                self.assertEqual('success', current.outcome)
        row = self.p.db.execute('SELECT outcome,stop_reason FROM runs').fetchone()
        self.assertEqual(('success', None), tuple(row))
        self.assertEqual('task_budget_exhausted', self.p.db.execute(
            "SELECT outcome FROM stages WHERE task='slow'").fetchone()[0])

    def test_task_stage_deadline_yields_and_global_deadline_still_stops_all(self):
        self.p.config['timeouts'] = {'stage_seconds': 5}
        self.p.config['budgets'] = {'task_seconds': 20, 'run_seconds': 30}
        clock = [100.0]
        with patch('telemetry.time.monotonic', side_effect=lambda: clock[0]):
            with run(self.p, 'stages') as current:
                with self.assertRaisesRegex(TaskBudgetExhausted, 'task_stage_time'):
                    with stage('verification', 'slow'):
                        clock[0] += 6
                with stage('planning', 'other'):
                    self.assertEqual(5, current.remaining())
                clock[0] = 131
                with self.assertRaises(BudgetExhausted) as caught:
                    current.remaining()
                self.assertNotIsInstance(caught.exception, TaskBudgetExhausted)

    def test_lifetime_task_call_limit_does_not_block_another_task(self):
        self.p.config['budgets'] = {'task_model_calls': 1, 'run_model_calls': 3}
        with run(self.p, 'calls') as current:
            current.reserve_call('one', 'plan', 'model', 'test')
            with self.assertRaisesRegex(TaskBudgetExhausted, 'task_model_calls'):
                current.reserve_call('one', 'plan', 'model', 'test')
            current.reserve_call('two', 'plan', 'model', 'test')
            self.assertIsNone(current.reason)
        self.assertEqual(2, self.p.db.execute('SELECT count(*) FROM model_calls').fetchone()[0])

    def test_clipped_command_timeout_stays_local_to_task(self):
        self.p.config['budgets'] = {'task_seconds': .15, 'run_seconds': 30}
        with run(self.p, 'child') as current:
            with self.assertRaises(TaskBudgetExhausted):
                with stage('testing', 'slow'):
                    execute([sys.executable, '-c', 'import time; time.sleep(5)'], timeout=10)
            with stage('planning', 'other'):
                self.assertGreater(current.remaining(), 0)
            self.assertIsNone(current.reason)

    def test_daily_call_cap_wins_when_task_cap_is_exhausted_at_same_time(self):
        self.p.config['budgets'] = {'task_model_calls': 1, 'daily_model_calls': 1, 'run_model_calls': 3}
        with run(self.p, 'simultaneous-caps') as current:
            current.reserve_call('one', 'plan', 'model', 'test')
            with self.assertRaisesRegex(BudgetExhausted, 'daily_model_calls_exhausted') as caught:
                current.reserve_call('one', 'plan', 'model', 'test')
            self.assertNotIsInstance(caught.exception, TaskBudgetExhausted)
            self.assertEqual('daily_model_calls_exhausted', current.reason)
            self.assertEqual(set(), current.paused_tasks)
        self.assertEqual(1, self.p.db.execute('SELECT count(*) FROM model_calls').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
