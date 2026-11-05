import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from contribute import Pipeline, dispatch
from telemetry import BudgetExhausted, maintenance_gaps, metrics, observe_pr


class PRMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config.json').write_text(json.dumps({'repositories': ['owner/repo']}))
        self.p = Pipeline(self.root)
        self.addCleanup(self.p.db.close)
        self.pr = {'number': 42, 'state': 'OPEN', 'title': 'Fix behavior',
                   'url': 'https://github.com/owner/repo/pull/42', 'headRefOid': 'published-head',
                   'ci': {'state': 'failed'},
                   'comments': [{'id': 1, 'body': 'Please fix the test', 'author_association': 'MEMBER',
                                 'user': {'type': 'User', 'login': 'maintainer'}}]}

    def link(self, state='PR_OPEN', repo='owner/repo'):
        task = self.p.hunt('owner/repo', 'local task')
        with self.p.db:
            self.p.db.execute('CREATE TABLE IF NOT EXISTS automation(task TEXT PRIMARY KEY,data TEXT NOT NULL)')
            self.p.db.execute('UPDATE tasks SET state=?,repo=? WHERE id=?', (state, repo, task))
            self.p.db.execute('INSERT OR REPLACE INTO automation VALUES (?,?)',
                              (task, json.dumps({'stage': state, 'pr_number': 42})))
        return task

    def test_unchanged_untracked_pr_remains_visible_without_adoption(self):
        # An issue with the same number is not a verified link to this PR.
        task = self.p.hunt('owner/repo', 'old failed patch')
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET number=42,state='FAILED' WHERE id=?", (task,))
        with patch.object(self.p, 'effective_repositories', return_value=['owner/repo']), \
                patch.object(self.p, 'followup_repository', return_value=[self.pr]):
            self.assertEqual([self.pr], self.p.followup())
            first = json.loads((self.root / 'runs/pr-maintenance.json').read_text())
            self.assertEqual([], self.p.followup())
            second = json.loads((self.root / 'runs/pr-maintenance.json').read_text())
        self.assertEqual(1, first['gap_count'])
        self.assertEqual(1, second['gap_count'])
        gap = second['gaps'][0]
        self.assertEqual('untracked_pr', gap['reason'])
        self.assertEqual('published-head', gap['head_sha'])
        self.assertEqual(['ci_failed', 'maintainer_response_observed'], gap['signals'])
        self.assertTrue(gap['observed_at'])
        self.assertEqual('FAILED', self.p.task(task)['state'])
        self.assertEqual([], gap['linked_tasks'])

    def test_terminal_link_is_reported_active_link_and_closed_pr_are_not(self):
        observe_pr(self.p.db, 'owner/repo', self.pr)
        task = self.link(state='FAILED', repo='Owner/Repo')
        report = maintenance_gaps(self.p.db)
        self.assertEqual('inactive_linked_task', report['gaps'][0]['reason'])
        self.assertEqual(task, report['gaps'][0]['linked_tasks'][0]['task'])
        self.link(state='PR_OPEN', repo='Owner/Repo')
        self.assertEqual([], maintenance_gaps(self.p.db)['gaps'])
        self.link(state='FAILED')
        observe_pr(self.p.db, 'owner/repo', {**self.pr, 'state': 'CLOSED'})
        self.assertEqual(0, maintenance_gaps(self.p.db)['open_observed_prs'])

    def test_metrics_and_status_expose_gap_without_mutation_or_remote_calls(self):
        observe_pr(self.p.db, 'owner/repo', {**self.pr, 'comments': [], 'ci': {'state': 'approval_required'}})
        before = list(self.p.db.iterdump())
        output = io.StringIO()
        with patch('contribute.gh') as remote, patch('autonomous.Autonomous.model') as model, \
                contextlib.redirect_stdout(output):
            report = metrics(self.root / 'state/tasks.db')
            dispatch(self.p, SimpleNamespace(action='status'))
        self.assertEqual(0, report['quality_all_time']['submitted_unique_prs'])
        self.assertEqual(1, report['pr_maintenance']['gap_count'])
        gap = report['pr_maintenance']['gaps'][0]
        self.assertEqual(['ci_approval_required'], gap['signals'])
        self.assertEqual(report['pr_maintenance'], json.loads(output.getvalue())['pr_maintenance'])
        self.assertEqual(before, list(self.p.db.iterdump()))
        remote.assert_not_called()
        model.assert_not_called()

    def test_legacy_database_is_not_migrated(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE tasks(id TEXT)')
            before = list(db.iterdump())
            self.assertEqual(0, maintenance_gaps(db)['gap_count'])
            self.assertEqual(before, list(db.iterdump()))

    def test_tick_observes_prs_before_model_budget_exhaustion(self):
        self.p.config['automation'] = {'enabled': True}
        order = []
        def exhausted():
            order.append('auto')
            raise BudgetExhausted('time_budget_exhausted')
        with patch.object(self.p, 'effective_repositories', return_value=['owner/repo']), \
                patch.object(self.p, 'followup', side_effect=lambda: order.append('followup') or [self.pr]), \
                patch.object(self.p, 'scan') as scan, \
                patch('autonomous.Autonomous') as controller, contextlib.redirect_stdout(io.StringIO()) as output:
            controller.return_value.run.side_effect = exhausted
            with self.assertRaisesRegex(BudgetExhausted, 'time_budget_exhausted'):
                dispatch(self.p, SimpleNamespace(action='tick', analyze=False))
        self.assertEqual(['followup', 'auto'], order)
        self.assertIn(self.pr['url'], output.getvalue())
        scan.assert_not_called()

    def test_tick_does_not_start_model_when_followup_exhausts_budget(self):
        self.p.config['automation'] = {'enabled': True}
        with patch.object(self.p, 'effective_repositories', return_value=['owner/repo']), \
                patch.object(self.p, 'followup', side_effect=BudgetExhausted('time_budget_exhausted')), \
                patch('autonomous.Autonomous') as controller:
            with self.assertRaises(BudgetExhausted):
                dispatch(self.p, SimpleNamespace(action='tick', analyze=False))
        controller.assert_not_called()


if __name__ == '__main__':
    unittest.main()
