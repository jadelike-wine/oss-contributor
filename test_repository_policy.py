import contextlib
import copy
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from autonomous import Autonomous
from contribute import ContributionPaused, Pipeline, now, save
from repository_policy import maintenance_notice, ready_issue
from test_scan import REPOSITORIES


class RepositoryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = json.loads(Path(__file__).with_name('config.json').read_text(encoding='utf-8'))
        self.config.setdefault('research', {})['batch_on_auto'] = False  # Isolate health tests from research dispatch.
        (self.root / 'config.json').write_text(json.dumps(self.config), encoding='utf-8')
        self.p = Pipeline(self.root)
        self.calls = []
        self.meta = {repo: {'full_name': repo, 'archived': False, 'disabled': False,
                          'has_issues': True, 'pushed_at': now(), 'default_branch': 'main',
                          'stargazers_count': 1} for repo in REPOSITORIES}
        self.readmes = {repo: '# Active project\nContributions welcome.' for repo in REPOSITORIES}

    def tearDown(self):
        self.p.db.close()
        self.temp.cleanup()

    def github(self, *args):
        self.calls.append(args)
        if args[:2] == ('api', 'graphql'):
            return {'data': {'repository': {'issues': {'nodes': []}}}}
        if args[0] == 'api':
            return copy.deepcopy(self.meta[args[1].removeprefix('repos/')])
        return []

    def command(self, args):
        endpoint = args[2]
        if endpoint.endswith('/readme'):
            return self.readmes[endpoint.removeprefix('repos/').removesuffix('/readme')]
        return 'Contribution guidelines'

    def network(self, github=None):
        stack = contextlib.ExitStack()
        stack.enter_context(patch('contribute.gh', side_effect=github or self.github))
        stack.enter_context(patch('contribute.command', side_effect=self.command))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        return stack

    def test_shared_targets_and_two_retired_identifiers(self):
        self.assertEqual(self.config['repositories'], REPOSITORIES)
        self.assertEqual(set(self.config['repository_checks']['retired']),
                         {'continuedev/continue', 'SWE-agent/SWE-agent'})
        with self.network():
            self.assertEqual(self.p.effective_repositories(), REPOSITORIES)
            self.assertEqual(self.p.effective_repositories(contribution=True), REPOSITORIES)
            self.assertEqual(self.p.scan(), 0)
            self.p.followup()
        scanned = [args[args.index('-R') + 1] for args in self.calls if args[0] == 'issue']
        followed = [args[args.index('-R') + 1] for args in self.calls if args[0] == 'pr' and '--author' in args]
        self.assertEqual(scanned, REPOSITORIES)
        self.assertEqual(followed, REPOSITORIES)
        self.assertEqual(sum(args == ('api', 'repos/' + REPOSITORIES[0]) for args in self.calls), 1)

    def test_health_changes_pause_only_affected_repo_and_recover_on_next_start(self):
        repo = REPOSITORIES[0]
        cases = [
            ({'archived': True}, None),
            ({'disabled': True}, None),
            ({'has_issues': False}, None),
            ({'full_name': 'new-owner/moved'}, None),
            ({'pushed_at': (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=181)).isoformat()}, None),
            ({'pushed_at': None}, None),
            ({}, 'This repository is no longer actively maintained.'),
            ({}, 'This project has moved to https://github.com/new/project'),
            ({}, 'We no longer accept new contributions.'),
        ]
        for metadata, readme in cases:
            with self.subTest(metadata=metadata, readme=readme):
                original = self.meta[repo].copy()
                self.meta[repo].update(metadata)
                self.readmes[repo] = readme or 'Active project'
                with self.network():
                    statuses = self.p.refresh_repository_status()
                    self.assertEqual(self.p.effective_repositories(), REPOSITORIES[1:])
                self.assertFalse(statuses[repo]['active'])
                self.assertTrue(statuses[repo]['reason'])
                self.meta[repo] = original
        self.readmes[repo] = 'Active again'
        self.p.db.close()
        self.p = Pipeline(self.root)
        with self.network():
            self.assertEqual(self.p.effective_repositories(), REPOSITORIES)

    def test_failed_check_never_reuses_previous_active_status(self):
        with self.network():
            self.p.refresh_repository_status()

        def broken(*args):
            if args == ('api', 'repos/' + REPOSITORIES[0]):
                raise RuntimeError('HTTP 403: rate limit exceeded')
            return self.github(*args)

        self.p.db.close()
        self.p = Pipeline(self.root)
        with self.network(broken):
            self.assertEqual(self.p.effective_repositories(), REPOSITORIES[1:])
        status = json.loads((self.root / 'state/repositories.json').read_text())['repositories'][REPOSITORIES[0]]
        self.assertIn('rate limit', status['reason'])
        self.assertFalse(status['contribute'])

    def test_unknown_board_permission_keeps_scan_and_pauses_goose_work(self):
        def no_scope(*args):
            if args[:2] == ('api', 'graphql'):
                raise RuntimeError('Missing read:project scope')
            return self.github(*args)

        with self.network(no_scope):
            self.assertEqual(self.p.effective_repositories(), REPOSITORIES)
            self.assertNotIn('aaif-goose/goose', self.p.effective_repositories(contribution=True))
            self.p.scan()
        self.assertTrue(any(args[0] == 'issue' and 'aaif-goose/goose' in args for args in self.calls))
        self.assertIn('read:project', self.p._repository_status['aaif-goose/goose']['reason'])

    def test_old_task_stages_scores_payloads_and_automation_are_unchanged(self):
        old = 'SWE-agent/SWE-agent'
        # Seed the historical format before the new configuration is applied.
        self.p.config['repositories'].append(old)
        key = self.p.hunt(old, 'historical task')
        engine = Autonomous(self.p)
        data = {'stage': 'REPAIR', 'attempts': {'REPAIR': 2}, 'evidence': 'previous execution'}
        engine.store(key, data, 'REPAIR')
        task_before = self.p.task(key)
        schema = [tuple(row) for row in self.p.db.execute('SELECT name,sql FROM sqlite_master ORDER BY name')]
        self.p.config['repositories'].remove(old)
        with self.network(), patch.object(engine, 'model') as model:
            self.assertNotIn(key, [row['id'] for row in engine.candidates()])
            self.assertEqual(engine.advance(key), 'REPAIR')
            with self.assertRaisesRegex(RuntimeError, 'Contribution paused'):
                self.p.analyze(key)
            model.assert_not_called()
        self.assertEqual(self.p.task(key), task_before)
        self.assertEqual(engine.load(key), data)
        self.assertEqual([tuple(row) for row in self.p.db.execute('SELECT name,sql FROM sqlite_master ORDER BY name')], schema)

    def test_tick_and_weekly_hunts_use_effective_targets(self):
        from contribute import main
        self.meta[REPOSITORIES[0]]['archived'] = True
        self.p.config['automation']['weekly_hunts'] = False
        for repo in REPOSITORIES:
            self.p.hunt(repo, 'candidate')
        engine = Autonomous(self.p)
        with self.network(), patch('contribute.Pipeline', return_value=self.p), \
                patch('sys.argv', ['contribute.py', 'tick']), patch('autonomous.Autonomous.run') as run:
            main()
            run.assert_called_once()
            self.assertNotIn(REPOSITORIES[0], [row['repo'] for row in engine.candidates()])
        self.p.config['automation']['weekly_hunts'] = True
        seen = []
        with self.network(), patch.object(self.p, 'hunt', side_effect=lambda repo, focus: seen.append(repo)), \
                patch.object(engine, 'advance', return_value='PLAN'):
            engine.run()
        self.assertEqual(seen, [repo for repo in REPOSITORIES[1:] if repo != 'aaif-goose/goose'])

    def test_followup_isolates_failure_and_retains_previous_observations(self):
        pr = {'number': 1, 'state': 'OPEN', 'url': 'old-pr', 'title': 'fix'}
        save(self.root / 'state/prs.json', {REPOSITORIES[0] + '#1': pr})

        def failure(*args):
            if args[0] == 'pr' and REPOSITORIES[0] in args:
                raise RuntimeError('network error')
            return self.github(*args)

        with self.network(failure):
            self.assertEqual(self.p.followup(), [])
        self.assertEqual(json.loads((self.root / 'state/prs.json').read_text()), {REPOSITORIES[0] + '#1': pr})
        self.assertTrue(any(args[0] == 'pr' and REPOSITORIES[-1] in args for args in self.calls))

    def test_retired_pr_is_still_observed_without_contribution(self):
        old = 'continuedev/continue'
        self.p.config['repositories'].append(old)
        key = self.p.hunt(old, 'published historical contribution')
        engine = Autonomous(self.p)
        data = {'stage': 'PR_OPEN', 'attempts': {}, 'pr_number': 1, 'evidence': 'preserved'}
        engine.store(key, data, 'PR_OPEN')
        self.p.config['repositories'].remove(old)
        save(self.root / 'state/prs.json', {old + '#1': {'number': 1, 'state': 'OPEN'}})

        def github(*args):
            if args[0] == 'pr' and old in args:
                return [{'number': 1, 'state': 'MERGED', 'title': 'fix', 'url': 'pr'}]
            return self.github(*args)

        with self.network(github):
            self.assertEqual(self.p.followup()[0]['state'], 'MERGED')
        self.assertNotIn(old, self.p.effective_repositories(contribution=True))
        self.assertEqual(json.loads((self.root / 'state/prs.json').read_text())[old + '#1']['state'], 'MERGED')
        self.assertEqual(self.p.task(key)['state'], 'MERGED')
        self.assertEqual(engine.load(key), {**data, 'stage': 'MERGED'})

    def test_goose_ready_gate_checks_exact_board_and_disables_automatic_issues(self):
        repo = 'aaif-goose/goose'
        key = self.p.hunt(repo, 'new finding')
        with self.network():
            with self.assertRaisesRegex(RuntimeError, 'submit an issue personally'):
                self.p.require_contribution(self.p.task(key))
            with self.p.db:
                self.p.db.execute('UPDATE tasks SET number=12 WHERE id=?', (key,))
            with patch('contribute.ready_issue', return_value=False):
                with self.assertRaisesRegex(RuntimeError, 'must be OPEN and Ready'):
                    self.p.prepare(key)
            with patch('contribute.ready_issue', return_value=True):
                self.p.require_contribution(self.p.task(key))
                with self.assertRaisesRegex(RuntimeError, 'personal code review'):
                    self.p.require_contribution(self.p.task(key), publishing=True)
        self.assertFalse((self.root / 'workspaces').exists())

    def test_review_reads_discovered_non_root_contribution_guide(self):
        repo = 'SWE-agent/mini-swe-agent'
        with self.network():
            self.p.refresh_repository_status()
        self.p._repository_status[repo]['contributing'] = f'https://api.github.com/repos/{repo}/contents/docs/contributing.md'
        work = self.root / 'work'
        (work / 'docs').mkdir(parents=True)
        (work / 'docs/contributing.md').write_text('Actual upstream policy', encoding='utf-8')
        with patch('autonomous.changed_files', return_value=[]):
            rules = Autonomous(self.p).rules({'repo': repo, 'workspace': str(work)})
        self.assertEqual(rules, {str(Path('docs/contributing.md')): 'Actual upstream policy'})

    def test_legacy_tick_policy_hold_preserves_task_stage(self):
        from contribute import main
        self.p.config['automation']['enabled'] = False
        key = self.p.hunt(REPOSITORIES[0], 'legacy candidate')
        with self.network(), patch('contribute.Pipeline', return_value=self.p), \
                patch('sys.argv', ['contribute.py', 'tick', '--analyze']), \
                patch.object(self.p, 'scan', return_value=0), patch.object(self.p, 'followup', return_value=[]), \
                patch.object(self.p, 'analyze', side_effect=ContributionPaused('policy changed')):
            main()
        self.assertEqual(self.p.task(key)['state'], 'DISCOVERED')


class NoticeAndBoardTests(unittest.TestCase):
    def test_notices_identify_current_project_not_dependencies(self):
        self.assertIsNone(maintenance_notice('owner/repo', 'The old dependency is unmaintained.'))
        self.assertIsNone(maintenance_notice('owner/repo', 'This project is actively maintained.'))
        self.assertTrue(maintenance_notice('continuedev/continue', '> _The `continuedev/continue` repository is no longer actively maintained._'))
        self.assertTrue(maintenance_notice('SWE-agent/SWE-agent', 'Most effort is on [mini](url),\nwhich has superseded SWE-agent.'))

    def test_ready_requires_open_issue_exact_project_and_complete_memberships(self):
        url = 'https://github.com/orgs/aaif-goose/projects/1'
        item = {'project': {'url': url}, 'fieldValueByName': {'name': 'Ready'}}
        issue = {'state': 'OPEN', 'projectItems': {'nodes': [item], 'pageInfo': {'hasNextPage': False}}}
        github = Mock(return_value={'data': {'repository': {'issue': issue}}})
        self.assertTrue(ready_issue('aaif-goose/goose', 12, url, github))
        item['project']['url'] = 'https://github.com/orgs/someone/projects/1'
        self.assertFalse(ready_issue('aaif-goose/goose', 12, url, github))
        item['project']['url'] = url
        item['fieldValueByName']['name'] = 'Accepted / design'
        self.assertFalse(ready_issue('aaif-goose/goose', 12, url, github))
        item['fieldValueByName']['name'] = 'Ready'
        issue['state'] = 'CLOSED'
        self.assertFalse(ready_issue('aaif-goose/goose', 12, url, github))
        issue['state'] = 'OPEN'
        issue['projectItems']['pageInfo']['hasNextPage'] = True
        with self.assertRaisesRegex(ValueError, 'truncated'):
            ready_issue('aaif-goose/goose', 12, url, github)
        self.assertFalse(ready_issue('aaif-goose/goose', None, url, github))


if __name__ == '__main__':
    unittest.main()
