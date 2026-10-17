import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from contribute import Pipeline, rank


REPOSITORIES = [
    'QwenLM/qwen-code', 'google-gemini/gemini-cli', 'anomalyco/opencode',
    'cline/cline', 'OpenHands/OpenHands', 'Kilo-Org/kilocode',
    'aaif-goose/goose', 'SWE-agent/mini-swe-agent',
]


class ScanTests(unittest.TestCase):
    def setUp(self):
        health = patch.object(Pipeline, 'effective_repositories', lambda p, contribution=False: list(p.config['repositories']))
        health.start()
        self.addCleanup(health.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = json.loads(Path(__file__).with_name('config.json').read_text(encoding='utf-8'))
        (self.root / 'config.json').write_text(json.dumps(self.config), encoding='utf-8')
        self.p = Pipeline(self.root)
        self.visited = []
        self.calls = []
        self.issues = {repo: [{
            'number': 12, 'title': 'Tool timeout', 'body': 'Steps to reproduce',
            'labels': [{'name': 'bug'}], 'assignees': [], 'updatedAt': 'today',
            'url': f'https://github.com/{repo}/issues/12',
        }] for repo in REPOSITORIES}
        self.prs = {repo: [] for repo in REPOSITORIES}

    def tearDown(self):
        self.p.db.close()
        self.temp.cleanup()

    def fake_gh(self, *args):
        self.calls.append(args)
        if args[0] == 'api':
            repo = args[1].removeprefix('repos/')
            self.visited.append(repo)
            return {'archived': False, 'full_name': repo, 'stargazers_count': 1,
                    'default_branch': 'main', 'pushed_at': 'today'}
        repo = args[args.index('-R') + 1]
        self.assertEqual(args[args.index('--state') + 1], 'open')
        self.assertEqual(args[args.index('--limit') + 1], '100' if args[0] == 'issue' else '200')
        return copy.deepcopy(self.issues[repo] if args[0] == 'issue' else self.prs[repo])

    def scan(self, gh=None, command=None):
        self.log = io.StringIO()
        with patch('contribute.gh', side_effect=gh or self.fake_gh), \
                patch('contribute.command', side_effect=command) as cmd, redirect_stderr(self.log):
            if command is None:
                cmd.return_value = 'Rules'
            return self.p.scan()

    def test_exact_allowlist_limits_origin_scores_sorting_and_repeat_scan(self):
        self.assertEqual(self.config['repositories'], REPOSITORIES)
        self.assertEqual(len(set(self.config['repositories'])), 8)
        self.assertEqual(self.config['scan_limit'], 100)
        self.issues[REPOSITORIES[0]][0]['labels'].append({'name': 'priority/p1'})
        self.assertEqual(self.scan(), 8)
        self.assertEqual(self.visited, REPOSITORIES)
        self.assertEqual(sum(args[0] == 'issue' for args in self.calls), 8)
        self.assertEqual(sum(args[0] == 'pr' for args in self.calls), 8)
        for repo in REPOSITORIES:
            task = self.p.task(repo.replace('/', '--') + '--12')
            self.assertEqual(task['repo'], repo)
            self.assertEqual(task['score'], rank(self.issues[repo][0])[0])
            self.assertIn(f'scan {repo}: OK', self.log.getvalue())
        report = (self.root / 'runs/queue.md').read_text(encoding='utf-8')
        self.assertLess(report.index('QwenLM--qwen-code--12'), report.index('cline--cline--12'))
        self.assertEqual(self.scan(), 0)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM tasks').fetchone()[0], 8)

    def test_failures_leave_other_repositories_a_turn_and_diagnostics(self):
        bad = REPOSITORIES[0]
        cases = [
            ('api', RuntimeError('HTTP 403: API rate limit exceeded')),
            ('issue', RuntimeError('HTTP 401: authentication required')),
            ('pr', subprocess.TimeoutExpired(['gh', 'pr', 'list'], 180)),
            ('api', OSError('network unavailable')),
            ('issue', json.JSONDecodeError('invalid JSON', '', 0)),
            ('api', {'archived': None}),
            ('issue', {'message': 'unexpected object'}),
            ('issue', [None]),
            ('issue', [{**self.issues[bad][0], 'number': '12'}]),
            ('issue', [{**self.issues[bad][0], 'labels': [{'name': None}]}]),
            ('pr', [{'number': 20, 'url': 'pr', 'closingIssuesReferences': None}]),
        ]
        for stage, failure in cases:
            with self.subTest(stage=stage, failure=repr(failure)):
                self.visited.clear()

                def fail(*args):
                    result = self.fake_gh(*args)
                    repo = args[1].removeprefix('repos/') if args[0] == 'api' else args[args.index('-R') + 1]
                    if repo == bad and args[0] == stage:
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                    return result

                self.scan(gh=fail)
                self.assertEqual(self.visited, REPOSITORIES)
                self.assertEqual(self.p.db.execute('SELECT count(*) FROM tasks').fetchone()[0], 7)
                self.assertIn(f'scan {bad}: FAILED', self.log.getvalue())
                self.assertIn(f'scan {REPOSITORIES[-1]}: OK', self.log.getvalue())
                event = self.p.db.execute('SELECT event FROM events WHERE task=? ORDER BY id DESC', ('scan:' + bad,)).fetchone()[0]
                self.assertIn('FAILED', event)
                self.assertEqual(self.p.db.execute('SELECT count(*) FROM locks').fetchone()[0], 0)

    def test_empty_and_archived_have_distinct_logs(self):
        self.issues[REPOSITORIES[0]] = []

        def archived(*args):
            result = self.fake_gh(*args)
            if args == ('api', 'repos/' + REPOSITORIES[1]):
                result['archived'] = True
            return result

        self.assertEqual(self.scan(gh=archived), 6)
        self.assertIn(f'scan {REPOSITORIES[0]}: EMPTY', self.log.getvalue())
        self.assertIn(f'scan {REPOSITORIES[1]}: SKIPPED (archived)', self.log.getvalue())

    def test_missing_guide_is_optional_and_stale_snapshot_is_removed(self):
        repo = REPOSITORIES[4]
        rules = self.root / 'runs/snapshots' / repo.replace('/', '--') / 'CONTRIBUTING.md'
        rules.parent.mkdir(parents=True)
        rules.write_text('obsolete rules', encoding='utf-8')

        def github(*args):
            if args == ('api', f'repos/{repo}/community/profile'):
                return {'files': {'contributing': None}}
            return self.fake_gh(*args)

        def command(args):
            if args[2] == f'repos/{repo}/contents/CONTRIBUTING.md':
                raise RuntimeError('gh failed (1): Not Found (HTTP 404)')
            return 'Rules'

        self.assertEqual(self.scan(gh=github, command=command), 8)
        self.assertFalse(rules.exists())
        self.assertIn(f'scan {repo}: WARNING no contributing guide', self.log.getvalue())
        self.assertIn(f'scan {repo}: OK', self.log.getvalue())

    def test_guide_outside_root_is_discovered(self):
        repo = REPOSITORIES[4]
        url = f'https://api.github.com/repos/{repo}/contents/.github/CONTRIBUTING.md'

        def github(*args):
            if args == ('api', f'repos/{repo}/community/profile'):
                return {'files': {'contributing': {'url': url}}}
            return self.fake_gh(*args)

        def command(args):
            if args[2] == f'repos/{repo}/contents/CONTRIBUTING.md':
                raise RuntimeError('Not Found (HTTP 404)')
            return 'Discovered rules' if args[2] == url else 'Rules'

        self.assertEqual(self.scan(gh=github, command=command), 8)
        rules = self.root / 'runs/snapshots' / repo.replace('/', '--') / 'CONTRIBUTING.md'
        self.assertEqual(rules.read_text(encoding='utf-8'), 'Discovered rules')

    def test_guide_auth_error_is_failure_not_missing_guide(self):
        def command(args):
            if args[2].startswith('repos/' + REPOSITORIES[0] + '/'):
                raise RuntimeError('HTTP 403: forbidden')
            return 'Rules'

        self.assertEqual(self.scan(command=command), 7)
        self.assertIn('FAILED (RuntimeError): HTTP 403', self.log.getvalue())
        self.assertNotIn('WARNING no contributing guide', self.log.getvalue())

    def test_malformed_community_profile_does_not_interrupt_batch(self):
        repo = REPOSITORIES[0]

        def github(*args):
            if args == ('api', f'repos/{repo}/community/profile'):
                return {'files': None}
            return self.fake_gh(*args)

        def command(args):
            if args[2] == f'repos/{repo}/contents/CONTRIBUTING.md':
                raise RuntimeError('Not Found (HTTP 404)')
            return 'Rules'

        self.assertEqual(self.scan(gh=github, command=command), 7)
        self.assertIn('FAILED (ValueError): Invalid community profile', self.log.getvalue())
        self.assertIn(f'scan {REPOSITORIES[-1]}: OK', self.log.getvalue())

    def test_repository_transaction_rolls_back_on_late_processing_failure(self):
        repo = REPOSITORIES[0]
        self.issues[repo].append({**self.issues[repo][0], 'number': 13, 'title': 'bad processing'})

        def failing_rank(issue):
            if issue['title'] == 'bad processing':
                raise ValueError('late invalid issue')
            return rank(issue)

        with patch('contribute.rank', side_effect=failing_rank):
            self.assertEqual(self.scan(), 7)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM tasks WHERE repo=?', (repo,)).fetchone()[0], 0)

    def test_legacy_ids_active_states_and_schema_survive_reopen_and_updates(self):
        repo = REPOSITORIES[0]
        key = self.p.hunt(repo, 'legacy issue task')
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET number=12,source='issue',state='PATCH_READY',workspace='existing',base_sha='base' WHERE id=?", (key,))
        schema = self.p.db.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()
        original = self.p.task(key)
        self.p.db.close()
        self.p = Pipeline(self.root)
        self.assertEqual(self.scan(), 8)
        self.assertEqual(self.scan(), 0)
        task = self.p.task(key)
        for field in ('id', 'repo', 'number', 'source', 'state', 'workspace', 'base_sha'):
            self.assertEqual(task[field], original[field])
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM tasks').fetchone()[0], 8)
        self.assertEqual(self.p.db.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall(), schema)
        self.issues[repo][0]['title'] = 'Updated report'
        self.assertEqual(self.scan(), 1)
        self.assertEqual(self.p.task(key)['title'], 'Updated report')
        self.assertEqual(self.p.task(key)['state'], 'PATCH_READY')

    def test_competing_pr_holds_issue_for_every_repository(self):
        for repo in REPOSITORIES:
            self.prs[repo] = [{'number': 20, 'url': f'https://github.com/{repo}/pull/20',
                              'body': 'Fixes #12', 'closingIssuesReferences': [{'number': 12}]}]
        self.assertEqual(self.scan(), 8)
        for task in self.p.db.execute('SELECT * FROM tasks'):
            self.assertEqual(task['state'], 'HOLD')
            self.assertIn(f"https://github.com/{task['repo']}/pull/20", task['holds'])


if __name__ == '__main__':
    unittest.main()
