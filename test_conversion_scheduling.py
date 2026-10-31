import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from autonomous import Autonomous
from contribute import Pipeline
from scheduling import (RunTaskBudget, canonical_repo, existing_hunt, find_issue_task,
                        issue_identity, migrate, priority_key, reconcile_issue_duplicates,
                        research_reassessment, reusable_scan_record, scan_key, task_lane)
from telemetry import BudgetExhausted, run


class SchedulingPrimitivesTests(unittest.TestCase):
    def test_six_implementation_slots_and_separate_maintenance_research(self):
        budget = RunTaskBudget({'automation': {'max_tasks_per_tick': 6},
                                'research': {'candidates_per_run': 2}})
        for lane, count in [('implementation', 6), ('maintenance', 6), ('research', 2)]:
            for number in range(count):
                self.assertTrue(budget.admit(str(number), lane))
                self.assertTrue(budget.admit(str(number), lane))
            self.assertFalse(budget.admit('overflow', lane))

    def test_explicit_lane_limits_do_not_raise_existing_total_model_budget(self):
        config = {'automation': {'max_tasks_per_tick': 6, 'implementation_tasks_per_tick': 2,
                                 'maintenance_tasks_per_tick': 0}, 'budgets': {'run_model_calls': 32}}
        budget = RunTaskBudget(config)
        self.assertTrue(budget.admit('a', 'implementation'))
        self.assertTrue(budget.admit('b', 'implementation'))
        self.assertFalse(budget.admit('c', 'implementation'))
        self.assertFalse(budget.admit('a', 'maintenance'))
        self.assertEqual(config['budgets']['run_model_calls'], 32)

    def test_feedback_transition_must_reserve_an_implementation_slot(self):
        task = {'number': 1}
        self.assertEqual('maintenance', task_lane(task, {'stage': 'PR_OPEN'}))
        self.assertEqual('implementation', task_lane(task, {'stage': 'REPAIR', 'pr_number': 2}))
        self.assertEqual('maintenance', task_lane({}, {'stage': 'PLAN'}, 'NEEDS_MAINTAINER_CONFIRMATION'))

    def test_delivery_precedes_new_research_and_ready_precedes_unconfirmed(self):
        candidates = [({}, {'stage': 'PLAN'}, None),
                      ({}, {'stage': 'PLAN'}, 'NEEDS_MAINTAINER_CONFIRMATION'),
                      ({}, {'stage': 'PLAN'}, 'READY_TO_IMPLEMENT'),
                      ({}, {'stage': 'REPAIR'}, None),
                      ({}, {'stage': 'PUBLISH'}, None),
                      ({}, {'stage': 'REPAIR', 'pr_number': 2}, None)]
        self.assertEqual(sorted(range(6), key=lambda i: priority_key(*candidates[i])), [5, 4, 3, 2, 1, 0])

    def test_canonical_identity_handles_case_and_verified_rename_only(self):
        aliases = {'Old/Name': 'New/Name'}
        self.assertEqual('new/name#12', issue_identity('Old/Name', 12, aliases))
        self.assertEqual('new/name#12', issue_identity('https://github.com/NEW/Name.git', 12))
        self.assertNotEqual(issue_identity('one/repo', 12), issue_identity('two/repo', 12))
        with self.assertRaises(ValueError):
            canonical_repo('owner/repo/../../secret')

    def test_research_reassessment_requires_material_complete_new_evidence(self):
        previous = {'scoped_source_digest': 'a', 'policy_digest': 'p'}
        self.assertEqual([], research_reassessment(previous, {'complete': True, 'week': '2026-W44', 'sha': 'new'}))
        self.assertEqual([], research_reassessment(previous, {'complete': False, 'policy_digest': 'changed'}))
        self.assertEqual(['policy_digest'], research_reassessment(previous, {'complete': True, 'policy_digest': 'changed'}))

    def test_cache_ttl_is_not_extended_by_cache_hits_and_errors_are_retried(self):
        record = {'repo': 'Owner/Repo', 'status': 'scanned', 'observed_at': '2026-10-31T00:00:00+00:00',
                  'scan_key': scan_key('owner/repo', 100), 'issues': 12, 'prs': 3, 'changed': 4}
        previous = {'repositories': [record]}
        hit = reusable_scan_record(previous, 'owner/repo', '2026-10-31T00:10:00+00:00', 3600, 100)
        self.assertEqual('cached', hit['status'])
        self.assertEqual(0, hit['changed'])
        self.assertEqual(12, hit['issues'])
        self.assertEqual(record['observed_at'], hit['observed_at'])
        self.assertIsNone(reusable_scan_record({'repositories': [hit]}, 'owner/repo', '2026-10-31T01:00:00+00:00', 3600, 100))
        self.assertIsNone(reusable_scan_record(previous, 'owner/repo', '2026-10-31T00:10:00+00:00', 3600, 10))
        record['status'] = 'unknown'
        self.assertIsNone(reusable_scan_record(previous, 'owner/repo', '2026-10-31T00:10:00+00:00', 3600, 100))


class ConversionRegressionTests(unittest.TestCase):
    """These tests exercise controller integration, not just helper functions."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {'repositories': ['Owner/Repo', 'Other/Repo'], 'scan_limit': 100,
                       'automation': {'enabled': True, 'max_tasks_per_tick': 6}}
        (self.root / 'config.json').write_text(json.dumps(self.config), encoding='utf-8')
        self.p = Pipeline(self.root)
        self.engine = Autonomous(self.p)
        self.health = patch.object(self.p, 'effective_repositories', return_value=self.config['repositories'])
        self.health.start()
        self.addCleanup(self.health.stop)

    def tearDown(self):
        self.p.db.close()
        self.temp.cleanup()

    def issue_task(self, repo, number, stage='PLAN', identifier=None):
        key = identifier or repo.replace('/', '--') + '--' + str(number)
        with self.p.db:
            self.p.db.execute('''INSERT INTO tasks(id,repo,number,source,title,score,state,holds,payload,updated)
                VALUES (?,?,?,'issue','Bounded reproducible regression',50,?,'[]','{}','2026-10-31')''',
                (key, repo, number, stage))
        self.engine.store(key, {'stage': stage, 'attempts': {}})
        return key

    def test_configured_six_tasks_reach_code_stage_in_same_run(self):
        keys = [self.issue_task(self.config['repositories'][i % 2], i + 1, 'REPAIR') for i in range(7)]
        seen = []
        with patch.object(self.p, 'require_contribution'), patch.object(self.p, 'report'), \
                patch.object(self.engine, 'repair', side_effect=lambda key, data: seen.append(key)):
            with run(self.p, 'quota_regression'):
                for key in keys:
                    self.engine.advance(key)
        self.assertEqual(keys[:6], seen)

    def test_failed_plan_does_not_take_the_only_code_opportunity(self):
        first = self.issue_task('Owner/Repo', 1)
        second = self.issue_task('Other/Repo', 2, 'REPAIR')
        seen = []
        with patch.object(self.p, 'require_contribution'), patch.object(self.p, 'report'), \
                patch.object(self.engine, 'plan', side_effect=RuntimeError('HTTP 503 service unavailable')), \
                patch.object(self.engine, 'repair', side_effect=lambda key, data: seen.append(key)):
            with run(self.p, 'service_regression'):
                self.engine.advance(first)
                self.engine.advance(second)
        self.assertEqual([second], seen)

    def test_week_boundary_reuses_existing_research_without_deleting_history(self):
        first = self.p.hunt('Owner/Repo', 'Investigate cancellation. Research batch 2026-W43')
        second = self.p.hunt('Owner/Repo', 'Investigate cancellation. Research batch 2026-W44')
        self.assertEqual(first, second)
        self.assertEqual(1, self.p.db.execute('SELECT count(*) FROM tasks').fetchone()[0])

    def test_scan_reuses_case_variant_issue_identity(self):
        original = self.issue_task('owner/repo', 12, 'REPAIR', 'historical-task')
        issue = {'number': 12, 'title': 'Value regression', 'body': 'repro', 'labels': [{'name': 'bug'}],
                 'assignees': [], 'url': 'https://github.com/Owner/Repo/issues/12'}
        def github(*args):
            if args[0] == 'api':
                return {'archived': False, 'full_name': 'Owner/Repo', 'stargazers_count': 1,
                        'default_branch': 'main', 'pushed_at': 'today'}
            return [copy.deepcopy(issue)] if args[0] == 'issue' else []
        with patch('contribute.gh', side_effect=github), patch('contribute.contributing_guide', return_value=('Rules', 'url')):
            self.p.scan_repository('Owner/Repo')
        self.assertEqual(1, self.p.db.execute('SELECT count(*) FROM tasks').fetchone()[0])
        self.assertEqual('REPAIR', self.p.task(original)['state'])

    def test_cached_policy_without_metadata_still_refreshes_repository_metadata(self):
        self.p._repository_evidence['Owner/Repo'] = ({}, 'Rules')
        calls = []
        def github(*args):
            calls.append(args)
            if args[0] == 'api':
                return {'archived': False, 'full_name': 'Owner/Repo', 'stargazers_count': 1,
                        'default_branch': 'main', 'pushed_at': 'today'}
            return []
        with patch('contribute.gh', side_effect=github):
            self.assertEqual((0, 0, 0), self.p.scan_repository('Owner/Repo'))
        self.assertEqual(1, sum(args[0] == 'api' for args in calls))

    def test_duplicate_migration_is_idempotent_preserves_rows_and_prefers_existing_work(self):
        first = self.issue_task('owner/repo', 12, 'PLAN', 'scan-task')
        second = self.issue_task('Owner/Repo', 12, 'PATCH_READY', 'research-task')
        self.p.event(first, 'Original discovery')
        before = [tuple(row) for row in self.p.db.execute('SELECT * FROM tasks ORDER BY id')]
        linked = reconcile_issue_duplicates(self.p.db)
        self.assertEqual([(first, second)], linked)
        self.assertEqual(linked, reconcile_issue_duplicates(self.p.db))
        migrate(self.p.db)
        self.assertEqual(before, [tuple(row) for row in self.p.db.execute('SELECT * FROM tasks ORDER BY id')])
        self.assertEqual(1, self.p.db.execute('SELECT count(*) FROM events').fetchone()[0])
        self.assertEqual(second, find_issue_task(self.p.db, 'OWNER/REPO', 12))
        self.assertEqual('ok', self.p.db.execute('PRAGMA integrity_check').fetchone()[0])

    def test_hunt_lookup_keeps_semantic_differences_for_review(self):
        first = self.p.hunt('Owner/Repo', 'Investigate cancellation. Research batch 2026-W43')
        self.assertEqual(first, existing_hunt(self.p.db, 'owner/repo', 'Investigate cancellation. Research batch 2026-W44'))
        self.assertIsNone(existing_hunt(self.p.db, 'owner/repo', 'Investigate session recovery. Research batch 2026-W44'))

    def test_scan_interruption_preserves_unvisited_success_cache(self):
        from contribute import now, save
        self.p.config['scanning'] = {'cache_seconds': 3600}
        previous = {'repositories': [{'repo': 'Other/Repo', 'status': 'scanned', 'observed_at': now(),
                                      'scan_key': scan_key('Other/Repo', 100), 'issues': 9, 'prs': 2, 'changed': 0}]}
        save(self.root / 'runs/scan-summary.json', previous)
        with patch.object(self.p, 'scan_repositories', return_value=self.config['repositories']), \
                patch.object(self.p, 'scan_repository', side_effect=BudgetExhausted('bounded run stopped')):
            with self.assertRaises(BudgetExhausted):
                self.p.scan()
        summary = json.loads((self.root / 'runs/scan-summary.json').read_text())
        preserved = next(record for record in summary['repositories'] if record['repo'] == 'Other/Repo')
        self.assertEqual('cached', preserved['status'])
        self.assertEqual(previous['repositories'][0]['observed_at'], preserved['observed_at'])
        self.assertEqual(9, preserved['issues'])

    def test_implementation_does_not_overwrite_persistent_research_rotation(self):
        task = self.issue_task('Owner/Repo', 1, 'REPAIR')
        self.p.research_turn('Other/Repo')
        with patch.object(self.p, 'require_contribution'), patch.object(self.p, 'report'), \
                patch.object(self.engine, 'repair', side_effect=lambda key, data: self.engine.move(key, data, 'SKIPPED')):
            self.engine.run()
        self.assertEqual('SKIPPED', self.engine.load(task)['stage'])
        self.assertEqual('Other/Repo', self.p.db.execute("SELECT last_repo FROM scheduler WHERE name='research'").fetchone()[0])


if __name__ == '__main__':
    unittest.main()
