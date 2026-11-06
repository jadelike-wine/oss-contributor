import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contribute import Pipeline, now, save
from issue_relations import classify_pr, github_identity


REPO = 'owner/repo'


def issue(number=12, **extra):
    return {'number': number, 'url': f'https://github.com/{REPO}/issues/{number}',
            'title': 'Regression', 'body': 'Steps to reproduce', 'labels': [],
            'assignees': [], 'state': 'OPEN', 'updatedAt': '2026-11-06T00:00:00Z', **extra}


def pr(body='Fixes #12', **extra):
    return {'number': 20, 'url': f'https://github.com/{REPO}/pull/20', 'title': 'fix: regression',
            'body': body, 'closingIssuesReferences': [], 'state': 'OPEN', **extra}


class RelationTests(unittest.TestCase):
    def test_cross_repository_markdown_and_qualified_number_are_not_local(self):
        for body in ('[#12](https://github.com/other/repo/issues/12)', 'other/repo#12',
                     'Fixes https://github.com/other/repo/issues/12'):
            with self.subTest(body=body):
                self.assertIsNone(classify_pr(issue(), pr(body), REPO))

    def test_exact_identity_prevents_url_prefix_and_fragment_matches(self):
        for body in ('Fixes https://github.com/owner/repo/issues/123',
                     'https://github.com/owner/repo/pull/50#12',
                     'https://example.com/#12', 'Fixes #123',
                     'https://github.com/owner/repo/issues/12other'):
            with self.subTest(body=body):
                self.assertIsNone(classify_pr(issue(), pr(body), REPO))
        self.assertEqual(('owner/repo', 12), github_identity('https://github.com/Owner/Repo/issues/12?x=1#comment'))

    def test_exact_closing_requires_same_repository_identity(self):
        for body in ('Fixes #12', 'Closes owner/repo#12',
                     'Resolves [#12](https://github.com/Owner/Repo/issues/12)',
                     'Fixes https://github.com/owner/repo/issues/12'):
            self.assertEqual('competing', classify_pr(issue(), pr(body), REPO)['relation'])
        self.assertEqual('competing', classify_pr(issue(), pr('', closingIssuesReferences=[
            {'number': 12, 'url': issue()['url']}]), REPO)['relation'])
        self.assertIsNone(classify_pr(issue(), pr('', closingIssuesReferences=[
            {'number': 12, 'url': 'https://github.com/other/repo/issues/12'}]), REPO))
        self.assertEqual('scope_review', classify_pr(issue(), pr('', closingIssuesReferences=[{'number': 12}]), REPO)['relation'])

    def test_mentions_partial_and_test_only_need_scope_review(self):
        for body in ('Related: #12', 'Test-only. Fixes #12', 'Part of #12',
                     '#12 is partially addressed; remaining scope needs a follow-up.',
                     'Fixes #12. This does not fix the other path.'):
            self.assertEqual('scope_review', classify_pr(issue(), pr(body), REPO)['relation'])
        self.assertEqual('scope_review', classify_pr(issue(), pr(title='test(core): regression'), REPO)['relation'])
        self.assertEqual('inactive', classify_pr(issue(), pr(state='CLOSED'), REPO)['relation'])
        self.assertEqual('competing', classify_pr(issue(), pr('Fixes #12\n\n#13 is partially addressed.'), REPO)['relation'])

    def test_comment_and_code_examples_do_not_create_competition(self):
        self.assertIsNone(classify_pr(issue(), pr('```\nFixes #12\n```'), REPO))
        self.assertIsNone(classify_pr(issue(), pr('<!-- Fixes #12 -->'), REPO))


class ReassessmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {'repositories': [REPO], 'scan_limit': 100, 'feature_requests': {'enabled': True},
                       'scanning': {'cache_seconds': 0}, 'repository_checks': {}}
        (self.root / 'config.json').write_text(json.dumps(self.config), encoding='utf-8')
        self.pipeline = Pipeline(self.root)
        self.issues = [issue()]
        self.prs = [pr()]
        self.details = {12: issue()}
        self.pr_details = {20: pr()}
        self.calls = []
        self.github_patch = patch('contribute.gh', side_effect=self.github)
        self.github_patch.start()
        self.guide_patch = patch('contribute.contributing_guide', return_value=('Rules', None))
        self.guide_patch.start()
        self.addCleanup(self.github_patch.stop)
        self.addCleanup(self.guide_patch.stop)

    def tearDown(self):
        self.pipeline.db.close()
        self.temp.cleanup()

    def github(self, *args):
        self.calls.append(args)
        if args[0] == 'api':
            return {'archived': False, 'full_name': REPO, 'stargazers_count': 1,
                    'default_branch': 'main', 'pushed_at': now()}
        if args[1] == 'list':
            return copy.deepcopy(self.issues if args[0] == 'issue' else self.prs)
        result = (self.details if args[0] == 'issue' else self.pr_details)[int(args[2])]
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    def scan(self, **options):
        with self.pipeline.db:
            return self.pipeline.scan_repository(REPO, **options)

    def task(self):
        return self.pipeline.task('owner--repo--12')

    def test_single_fresh_reassessment_preserves_history_and_scope_hint(self):
        self.scan()
        self.assertEqual('HOLD', self.task()['state'])
        self.prs = [pr('Test-only. Adds regression coverage for #12.')]
        self.scan(issue_numbers=[12])
        self.assertEqual('DISCOVERED', self.task()['state'])
        payload = json.loads(self.task()['payload'])
        self.assertTrue(payload['_scan_relations']['scope_review_required'])
        event = self.pipeline.db.execute('SELECT event FROM events WHERE task=?', (self.task()['id'],)).fetchone()[0]
        self.assertIn('existing PR:', event)
        self.assertIn('scope_review', event)
        with self.pipeline.db:
            self.pipeline.db.execute("UPDATE tasks SET state='SKIPPED'")
        self.scan(issue_numbers=[12])
        self.assertEqual('SKIPPED', self.task()['state'])

    def test_capped_pr_list_requires_exact_old_pr_status_before_releasing(self):
        self.scan()
        self.prs = []
        self.pr_details[20] = RuntimeError('network unavailable')
        self.scan(issue_numbers=[12])
        self.assertEqual('HOLD', self.task()['state'])
        self.assertEqual([pr()['url']], json.loads(self.task()['payload'])['_scan_relations']['unknown_prs'])
        self.pr_details[20] = pr(state='CLOSED')
        self.scan(issue_numbers=[12])
        self.assertEqual('DISCOVERED', self.task()['state'])
        self.assertEqual('inactive', json.loads(self.task()['payload'])['_scan_relations']['prs'][0]['relation'])

    def test_missing_pr_read_budget_preserves_unknown_hold(self):
        self.scan()
        self.prs = []
        self.pipeline.config['scanning']['linked_pr_refresh_limit'] = 0
        self.scan(issue_numbers=[12])
        self.assertEqual('HOLD', self.task()['state'])
        self.assertFalse(any(call[:2] == ('pr', 'view') for call in self.calls))

    def test_stale_hold_outside_recent_window_is_refreshed_with_bounded_reads(self):
        self.issues = [issue(12, assignees=[{'login': 'other'}])]
        self.prs = []
        self.scan()
        with self.pipeline.db:
            self.pipeline.db.execute("UPDATE tasks SET updated='2000-01-01T00:00:00+00:00'")
        self.issues = [issue(99)]
        self.scan()
        self.assertEqual('DISCOVERED', self.task()['state'])
        self.assertEqual(1, sum(call[:2] == ('issue', 'view') for call in self.calls))
        evidence = json.loads((self.root / 'runs/snapshots/owner--repo/issue-refresh.json').read_text())
        self.assertEqual([12], evidence['refreshed_held_issues'])
        self.assertFalse(evidence['backlog_complete'])

    def test_failed_stale_observation_keeps_old_hold_and_recent_progress(self):
        self.scan()
        with self.pipeline.db:
            self.pipeline.db.execute("UPDATE tasks SET updated='2000-01-01T00:00:00+00:00'")
        self.issues = [issue(99)]
        self.details[12] = RuntimeError('network unavailable')
        self.scan()
        self.assertEqual('HOLD', self.task()['state'])
        self.assertEqual('DISCOVERED', self.pipeline.task('owner--repo--99')['state'])
        evidence = json.loads((self.root / 'runs/snapshots/owner--repo/issue-refresh.json').read_text())
        self.assertEqual('unknown', evidence['errors'][0]['status'])

    def test_exact_refresh_rejects_wrong_issue_or_unknown_state(self):
        self.details[12] = issue(13)
        with self.assertRaisesRegex(ValueError, 'exact issue'):
            self.scan(issue_numbers=[12])
        self.assertEqual(0, self.pipeline.db.execute('SELECT count(*) FROM tasks').fetchone()[0])

    def enqueue(self, number=12):
        self.pipeline.db.execute('''CREATE TABLE IF NOT EXISTS issue_refresh_queue (
            task TEXT PRIMARY KEY,repo TEXT,number INTEGER,reason TEXT,queued_at TEXT,
            status TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,last_attempted_at TEXT,
            retry_after TEXT,last_error TEXT,completed_at TEXT)''')
        self.pipeline.db.execute('INSERT INTO issue_refresh_queue(task,repo,number,reason,queued_at) VALUES (?,?,?,?,?)',
            (f'owner--repo--{number}', REPO, number, 'Scope review audit', now()))
        self.pipeline.db.commit()

    def test_queue_prioritizes_fresh_hold_and_keeps_completion_history(self):
        self.scan()
        self.enqueue()
        self.issues = [issue(99)]
        self.prs = [pr('Test-only. Coverage for #12')]
        self.scan()
        self.assertEqual('DISCOVERED', self.task()['state'])
        row = self.pipeline.db.execute('SELECT * FROM issue_refresh_queue').fetchone()
        self.assertEqual('completed', row['status'])
        self.assertEqual(1, row['attempts'])
        self.assertTrue(row['completed_at'])
        self.scan()
        self.assertEqual(1, self.pipeline.db.execute('SELECT attempts FROM issue_refresh_queue').fetchone()[0])

    def test_queue_failure_stays_unknown_and_preserves_hold_until_fresh_read(self):
        self.scan()
        self.enqueue()
        with self.pipeline.db:
            self.pipeline.db.execute("UPDATE tasks SET updated='2000-01-01T00:00:00+00:00'")
        self.issues = [issue(99)]
        self.details[12] = RuntimeError('network unavailable')
        self.scan()
        row = self.pipeline.db.execute('SELECT * FROM issue_refresh_queue').fetchone()
        self.assertEqual('unknown', row['status'])
        self.assertEqual('HOLD', self.task()['state'])
        self.assertGreater(row['retry_after'], now())
        attempts = len([call for call in self.calls if call[:2] == ('issue', 'view')])
        self.scan()
        self.assertEqual(attempts, len([call for call in self.calls if call[:2] == ('issue', 'view')]))
        self.details[12] = issue()
        self.prs = [pr('Related: #12')]
        with self.pipeline.db:
            self.pipeline.db.execute("UPDATE issue_refresh_queue SET retry_after='2000-01-01T00:00:00+00:00'")
        self.scan()
        row = self.pipeline.db.execute('SELECT * FROM issue_refresh_queue').fetchone()
        self.assertEqual('completed', row['status'])
        self.assertEqual(2, row['attempts'])
        self.assertEqual('DISCOVERED', self.task()['state'])
        self.assertTrue(self.pipeline.db.execute("SELECT 1 FROM events WHERE event LIKE 'Queued issue refresh unknown:%'").fetchone())

    def test_queue_recent_issue_completes_but_unknown_pr_preserves_queue(self):
        self.scan()
        self.enqueue()
        self.prs = []
        self.pr_details[20] = RuntimeError('network unavailable')
        self.scan()
        row = self.pipeline.db.execute('SELECT * FROM issue_refresh_queue').fetchone()
        self.assertEqual('unknown', row['status'])
        self.assertEqual('HOLD', self.task()['state'])
        self.pr_details[20] = pr(state='CLOSED')
        self.scan(issue_numbers=[12])
        self.assertEqual('completed', self.pipeline.db.execute('SELECT status FROM issue_refresh_queue').fetchone()[0])

    def test_exhausted_scan_recovers_after_cooldown_with_one_probe_and_fairness(self):
        repos = [REPO, 'other/repo', 'healthy/repo']
        save(self.root / 'runs/scan-summary.json', {'repositories': [
            {'repo': repo, 'status': 'unknown', 'failure_attempts': 3, 'retry_exhausted': True,
             'retry_after': '2000-01-01T00:00:00+00:00', 'error': 'network unavailable'} for repo in repos[:2]]})
        calls = []
        def scan_repo(repo):
            calls.append(repo)
            if repo == REPO:
                raise RuntimeError('network unavailable')
            return 0, 0, 0
        with patch.object(self.pipeline, 'scan_repositories', return_value=repos), \
                patch.object(self.pipeline, 'scan_repository', side_effect=scan_repo):
            self.pipeline.scan(max_repositories=2)
            self.assertEqual([REPO, 'healthy/repo'], calls)
            calls.clear()
            self.pipeline.scan(max_repositories=2)
            self.assertEqual(['other/repo', 'healthy/repo'], calls)
        summary = json.loads((self.root / 'runs/scan-summary.json').read_text())
        recovered = next(r for r in summary['repositories'] if r['repo'] == 'other/repo')
        self.assertEqual('scanned', recovered['status'])
        self.assertNotIn('retry_exhausted', recovered)
        failing = next(r for r in summary['repositories'] if r['repo'] == REPO)
        self.assertTrue(failing['retry_exhausted'])
        self.assertGreater(failing['retry_after'], now())

    def test_recovery_reserves_slot_even_when_healthy_repositories_lead_rotation(self):
        repos = ['healthy/repo', REPO, 'older/repo']
        save(self.root / 'runs/scan-summary.json', {'repositories': [
            {'repo': REPO, 'status': 'unknown', 'failure_attempts': 3, 'retry_exhausted': True,
             'recovery_probes': 2, 'retry_after': '2000-01-01T00:00:00+00:00'},
            {'repo': 'older/repo', 'status': 'unknown', 'failure_attempts': 3, 'retry_exhausted': True,
             'recovery_probes': 1, 'retry_after': '2001-01-01T00:00:00+00:00'}]})
        calls = []
        def scan_repo(repo):
            calls.append(repo)
            if repo != 'healthy/repo':
                raise RuntimeError('network unavailable')
            return 0, 0, 0
        with patch.object(self.pipeline, 'scan_repositories', return_value=repos), \
                patch.object(self.pipeline, 'scan_repository', side_effect=scan_repo):
            self.pipeline.scan(max_repositories=1)
            self.assertEqual(['older/repo'], calls)
            calls.clear()
            self.pipeline.scan(max_repositories=1)
            self.assertEqual(['healthy/repo'], calls)
            calls.clear()
            self.pipeline.scan(max_repositories=1)
            self.assertEqual([REPO], calls)

    def test_targeted_scan_preserves_full_scope_and_unrequested_checkpoints(self):
        from scheduling import scan_key
        repos = ['healthy/repo', REPO, 'not-yet/repo']
        prior = {'repo': 'healthy/repo', 'status': 'scanned', 'observed_at': now(),
                 'scan_key': scan_key('healthy/repo', 100), 'issues': 11, 'prs': 2, 'changed': 4}
        save(self.root / 'runs/scan-summary.json', {'repositories': [prior]})
        with patch.object(self.pipeline, 'scan_repositories', return_value=repos), \
                patch.object(self.pipeline, 'scan_repository', return_value=(1, 3, 2)) as scan:
            self.pipeline.scan(max_repositories=1, force=True, only_repositories=[REPO.upper()])
            scan.assert_called_once_with(REPO)
        summary = json.loads((self.root / 'runs/scan-summary.json').read_text())
        self.assertEqual(3, summary['planned_repositories'])
        self.assertEqual(set(repos), {r['repo'] for r in summary['repositories']})
        untouched = next(r for r in summary['repositories'] if r['repo'] == 'healthy/repo')
        self.assertEqual(prior, {k: v for k, v in untouched.items() if k != 'requested'})
        self.assertFalse(untouched['requested'])
        self.assertEqual(1, summary['totals']['scanned_repositories'])
        self.assertEqual(3, summary['totals']['issues'])
        self.assertTrue(summary['requested_completed'])
        self.assertFalse(summary['completed'])
        self.pipeline.config['scanning']['cache_seconds'] = 3600
        with patch.object(self.pipeline, 'scan_repositories', return_value=repos), \
                patch.object(self.pipeline, 'scan_repository', return_value=(0, 0, 0)) as scan:
            self.pipeline.scan(only_repositories=['healthy/repo'])
            scan.assert_not_called()

    def test_targeted_scan_rejects_out_of_pool_before_changing_checkpoint(self):
        prior = {'repositories': [], 'checkpoint': 'preserve'}
        save(self.root / 'runs/scan-summary.json', prior)
        with patch.object(self.pipeline, 'scan_repositories', return_value=[REPO]), \
                patch.object(self.pipeline, 'scan_repository') as scan:
            with self.assertRaisesRegex(ValueError, 'complete verified research pool'):
                self.pipeline.scan(only_repositories=['foreign/repo'])
            scan.assert_not_called()
        self.assertEqual(prior, json.loads((self.root / 'runs/scan-summary.json').read_text()))


if __name__ == '__main__':
    unittest.main()
