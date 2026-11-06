"""Feature delivery integration with temporary SQLite and fake external services.

The execution fixture uses a temporary Git repository and bare remote only.
No production state, hosted model, GitHub write, or live repository is accessed.
"""
import contextlib
import copy
import json
from pathlib import Path
import platform
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from autonomous import Autonomous
from contribute import ContributionPaused, Pipeline
import feature_request_flow as features
from scheduling import RunTaskBudget
import test_autonomous as execution_fixture


class FeatureSchedulingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {
            'repositories': ['one/repo', 'two/repo', 'three/repo', 'four/repo'],
            'feature_requests': {'enabled': True, 'candidates_per_tick': 2},
            'automation': {'enabled': True, 'implementation_tasks_per_tick': 8,
                           'maintenance_tasks_per_tick': 2, 'max_stage_attempts': 3},
        }
        self.write_config()
        health = patch.object(Pipeline, 'effective_repositories',
                              lambda p, contribution=False: list(p.config['repositories']))
        health.start()
        self.addCleanup(health.stop)
        self.p = Pipeline(self.root)
        self.addCleanup(lambda: self.p.db.close())
        self.engine = Autonomous(self.p)

    def write_config(self):
        (self.root / 'config.json').write_text(json.dumps(self.config), encoding='utf-8')

    def task(self, number, repo='one/repo', *, feature=True, state='DISCOVERED',
             stage='PLAN', holds=None, pr=False, source='issue'):
        key = repo.replace('/', '--') + '--' + str(number)
        issue = {'number': number, 'title': ('Feature request: export' if feature else 'Bug: wrong value'),
                 'body': 'Bounded behavior with acceptance tests.', 'labels': [{'name': 'enhancement' if feature else 'bug'}],
                 'assignees': [], 'state': 'open', 'html_url': f'https://github.com/{repo}/issues/{number}'}
        with self.p.db:
            self.p.db.execute('INSERT INTO tasks(id,repo,number,source,title,url,score,state,holds,payload,updated) '
                              'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                              (key, repo, number if source == 'issue' else None, source, issue['title'],
                               issue['html_url'], 50, state, json.dumps(holds or []), json.dumps(issue), '2026-11-05'))
        data = {'stage': stage, 'attempts': {}}
        if pr:
            data['pr_number'] = number + 1000
        self.engine.store(key, data)
        return key

    def dispatch(self, budget=None, attempted=None, policy=None):
        budget = budget or RunTaskBudget(self.p.config)
        attempted = attempted if attempted is not None else set()
        seen = []
        with patch.object(self.p, 'require_contribution', side_effect=policy), \
                patch.object(self.engine, 'advance', side_effect=lambda key: seen.append(key)):
            self.engine.dispatch_candidates(self.engine.candidates(), budget, attempted)
        return seen, budget, attempted

    def restart(self):
        self.p.db.close()
        self.p = Pipeline(self.root)
        self.engine = Autonomous(self.p)

    def test_only_sole_legacy_feature_hold_is_previewed_without_mutation(self):
        allowed = self.task(1, state='HOLD', holds=['feature needs maintainer approval'])
        self.task(2, state='HOLD', holds=['feature needs maintainer approval', 'assigned'])
        self.task(3, state='HOLD', holds=['security'])
        self.task(4, feature=False, state='HOLD', holds=['feature needs maintainer approval'])
        self.task(5, state='HOLD', holds=['feature needs maintainer approval'], source='hunt')
        for i, terminal in enumerate(('FAILED', 'SKIPPED', 'CLOSED', 'MERGED'), 6):
            self.task(i, state=terminal, holds=['feature needs maintainer approval'])
        before = [tuple(r) for r in self.p.db.execute('SELECT * FROM tasks ORDER BY id')]
        self.assertEqual([allowed], [r['id'] for r in self.engine.candidates()])
        self.assertEqual(before, [tuple(r) for r in self.p.db.execute('SELECT * FROM tasks ORDER BY id')])
        self.assertEqual(0, self.p.db.execute('SELECT count(*) FROM events').fetchone()[0])

    def test_zero_feature_quota_suppresses_feature_starts_but_keeps_bug_candidates(self):
        self.task(1, state='HOLD', holds=['feature needs maintainer approval'])
        self.task(2)
        bug = self.task(3, feature=False)
        self.p.config['feature_requests']['candidates_per_tick'] = 0
        self.assertEqual([bug], [r['id'] for r in self.engine.candidates()])
        self.p.config['feature_requests']['enabled'] = False
        self.p.config['feature_requests']['candidates_per_tick'] = 2
        self.assertEqual([bug], [r['id'] for r in self.engine.candidates()])
        seen, _, _ = self.dispatch()
        self.assertEqual([bug], seen, 'Disabling features must not route them through generic bug planning')

    def test_published_pr_and_active_work_precede_new_feature_plans(self):
        feature = self.task(1)
        bug = self.task(2, feature=False)
        active = self.task(3, state='REPAIR', stage='REPAIR')
        published = self.task(4, state='PR_OPEN', stage='PR_OPEN', pr=True)
        self.assertEqual([published, active, feature, bug], [r['id'] for r in self.engine.candidates()])

    def test_two_feature_attempts_share_eight_implementation_slots(self):
        published = self.task(1, state='PR_OPEN', stage='PR_OPEN', pr=True)
        active = self.task(2, state='REPAIR', stage='REPAIR', feature=False)
        feature_keys = [self.task(i) for i in (10, 11, 12)]
        bug_keys = [self.task(i, feature=False) for i in range(20, 26)]
        seen, budget, _ = self.dispatch()
        self.assertEqual([published, active, *feature_keys[:2], *bug_keys[:5]], seen)
        self.assertEqual(set(feature_keys[:2]), budget.feature_requests)
        self.assertEqual(8, len(budget.used['implementation']))
        self.assertEqual({published}, budget.used['maintenance'])
        self.assertNotIn(feature_keys[2], seen)
        self.assertNotIn(bug_keys[-1], seen)

    def test_second_dispatch_does_not_reset_feature_quota_or_repeat_attempts(self):
        keys = [self.task(i) for i in range(1, 5)]
        seen, budget, attempted = self.dispatch()
        again, _, _ = self.dispatch(budget, attempted)
        self.assertEqual(keys[:2], seen)
        self.assertEqual([], again)
        self.assertEqual(2, len(budget.used['implementation']))

    def test_policy_blocked_first_issue_yields_within_repo_after_restart(self):
        self.p.config['feature_requests']['candidates_per_tick'] = 1
        self.config['feature_requests']['candidates_per_tick'] = 1
        self.write_config()
        first, second = self.task(1), self.task(2)
        seen, budget, _ = self.dispatch(policy=lambda task: (_ for _ in ()).throw(ContributionPaused('Design pending')))
        self.assertEqual([], seen)
        self.assertEqual({first}, budget.feature_requests)
        self.assertEqual({first}, budget.used['implementation'])
        self.restart()
        self.assertEqual(second, self.engine.candidates()[0]['id'])
        seen, _, _ = self.dispatch()
        self.assertEqual([second], seen)
        self.assertEqual('DISCOVERED', self.p.task(first)['state'])

    def test_feature_repository_rotation_survives_restart(self):
        keys = [self.task(1, repo=repo) for repo in self.config['repositories']]
        seen, _, _ = self.dispatch()
        self.assertEqual(keys[:2], seen)
        self.restart()
        seen, _, _ = self.dispatch()
        self.assertEqual(keys[2:], seen)

    def test_feature_labeled_pr_maintenance_does_not_spend_feature_attempts(self):
        published = self.task(1, state='PR_OPEN', stage='PR_OPEN', pr=True)
        features_to_start = [self.task(i) for i in (2, 3)]
        seen, budget, _ = self.dispatch()
        self.assertEqual([published, *features_to_start], seen)
        self.assertEqual(set(features_to_start), budget.feature_requests)
        self.assertEqual({published}, budget.used['maintenance'])

    def test_waiting_feature_rechecks_share_the_two_attempt_implementation_budget(self):
        published = self.task(1, state='PR_OPEN', stage='PR_OPEN', pr=True)
        new = self.task(2)
        waiting = [self.task(i, state='WAITING_MAINTAINER', stage='WAITING_MAINTAINER') for i in (3, 4)]
        seen, budget, _ = self.dispatch()
        self.assertEqual([published, new, waiting[0]], seen)
        self.assertEqual({new, waiting[0]}, budget.feature_requests)
        self.assertEqual({new, waiting[0]}, budget.used['implementation'])
        self.assertNotIn(waiting[1], seen)


class FeatureExecutionIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Windows' first platform query may run `ver` through a shell. Cache
        # host metadata before the guard; all contribution commands remain guarded.
        platform.uname()
        real_popen = subprocess.Popen
        def local_only(args, *positional, **kwargs):
            if isinstance(args, str) or kwargs.get('shell'):
                raise AssertionError('Test forbids shell command strings')
            tool = Path(str(args[0])).stem.lower()
            if tool == 'gh':
                raise AssertionError('Test forbids every real gh subprocess')
            if tool == 'git' and 'push' in args:
                remote = args[args.index('push') + 1]
                root = getattr(getattr(self, 'fx', None), 'root', None)
                if root is None or not Path(remote).resolve().is_relative_to(root.resolve()):
                    raise AssertionError('Only a temporary local bare remote may receive a push')
            return real_popen(args, *positional, **kwargs)
        guard = patch('subprocess.Popen', side_effect=local_only)
        guard.start()
        self.addCleanup(guard.stop)
        # Compose the existing Git fixture; do not import/subclass its TestCase,
        # which would rediscover and duplicate its unrelated tests.
        self.fx = execution_fixture.AutonomousTests(methodName='runTest')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.addCleanup(self.fx.tearDown)
        self.p, self.engine, self.key = self.fx.p, self.fx.engine, self.fx.key
        self.p.config['feature_requests'] = {'enabled': True, 'candidates_per_tick': 2}
        self.issue = {'number': 1, 'state': 'open', 'title': 'Feature request: return the second value',
                      'body': 'Add the second value while preserving the public shape.',
                      'labels': [{'name': 'enhancement'}], 'assignees': [],
                      'html_url': 'https://github.com/owner/repo/issues/1'}
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET payload=?,title=? WHERE id=?',
                              (json.dumps(self.issue), self.issue['title'], self.key))
        self.fx.plan = {**self.fx.plan, 'acceptance_criteria': ['The public VALUE is 2 and the module remains importable.']}
        self.gets, self.order = [], []
        self.linked_prs = []

    def get(self, *args):
        self.gets.append(args)
        self.assertEqual('api', args[0])
        self.assertEqual(2, len(args), 'Only fake read endpoints are allowed')
        endpoint = args[1]
        if endpoint == 'repos/owner/repo/issues/1':
            return copy.deepcopy(self.issue)
        if endpoint == 'repos/owner/repo/issues/1/comments?per_page=100&page=1':
            return []
        if endpoint == 'repos/owner/repo/issues/1/timeline?per_page=100&page=1':
            return [{'event': 'cross-referenced', 'source': {'issue': {'number': pr['number'],
                    'pull_request': {'url': f"https://api.github.com/repos/owner/repo/pulls/{pr['number']}"}}}}
                    for pr in self.linked_prs]
        for pr in self.linked_prs:
            if endpoint == f"repos/owner/repo/pulls/{pr['number']}":
                return copy.deepcopy(pr)
        if endpoint == 'user':
            return {'login': 'bot'}
        self.fail('Unexpected feature GET: ' + endpoint)

    def policy(self, task, **kwargs):
        self.p._repository_evidence[task['repo']] = ({}, 'Small features welcome. Add tests and preserve compatibility.')

    def fake_command(self, args, **kwargs):
        if args[0] == 'gh':
            self.assertIn(args[:3], (['gh', 'repo', 'fork'], ['gh', 'pr', 'create'], ['gh', 'issue', 'create']),
                          'Unexpected external command')
        if args[:3] in (['gh', 'repo', 'fork'], ['gh', 'pr', 'create']) or args[:2] == ['git', 'push']:
            self.order.append(tuple(args[:3]))
        return self.fx.fake_command(args, **kwargs)

    @contextlib.contextmanager
    def mocks(self):
        with self.fx.mocks(), patch('feature_request_flow.gh', side_effect=self.get), \
                patch.object(self.p, 'require_contribution', side_effect=self.policy), \
                patch('autonomous.command', side_effect=self.fake_command):
            from autonomous import execute
            def tracked_execute(args, **kwargs):
                if args[:3] == ['gh', 'repo', 'fork']:
                    self.order.append(tuple(args[:3]))
                return execute(args, **kwargs)
            with patch('autonomous.execute', side_effect=tracked_execute):
                yield

    def plan(self):
        self.engine.plan(self.key, self.engine.load(self.key))
        return self.engine.load(self.key)

    def verified_publication(self):
        data = self.plan()
        self.engine.reproduce(self.key, data)
        self.engine.repair(self.key, data)
        self.engine.verify(self.key, data)
        self.engine.review(self.key, data)
        self.assertEqual('PUBLISH', data['stage'])
        return data

    def test_closed_feature_blocks_before_prepare_or_plan_model(self):
        self.issue['state'] = 'closed'
        with self.mocks(), patch.object(self.p, 'prepare') as prepare, patch.object(self.engine, 'model') as model:
            with self.assertRaisesRegex(ContributionPaused, 'open issue'):
                self.plan()
        prepare.assert_not_called()
        model.assert_not_called()
        self.assertEqual([], self.fx.created_issues)

    def test_feature_plan_requires_bounded_nonempty_acceptance_criteria(self):
        for invalid in (None, [], [''], [42], ['x' * 2001], ['criterion'] * 11):
            with self.subTest(criteria=invalid):
                self.fx.plan['acceptance_criteria'] = invalid
                with self.mocks():
                    with self.assertRaisesRegex(ValueError, 'acceptance criteria'):
                        self.plan()
                self.assertNotIn('feature_plan_scope_digest', self.engine.load(self.key))
        self.assertEqual('PLAN', self.engine.load(self.key)['stage'])

    def test_recipe_rejection_of_vague_feature_never_binds_scope_digest(self):
        self.fx.plan['acceptance_criteria'] = ['Make it nicer.']
        def judge(key, purpose, packet):
            if purpose == 'recipe-review':
                self.assertEqual(['Make it nicer.'], packet['plan']['acceptance_criteria'])
                return {'approved': False, 'reason': 'No observable acceptance behavior.'}
            return {'approved': True, 'reason': 'Fake bounded admission verdict.'}
        with self.mocks(), patch.object(self.engine, 'judge', side_effect=judge):
            with self.assertRaisesRegex(RuntimeError, 'Recipe review rejected'):
                self.plan()
        self.assertNotIn('feature_plan_scope_digest', self.engine.load(self.key))

    def test_scope_digest_is_bound_only_after_acceptance_recipe_review(self):
        reviewed = []
        def judge(key, purpose, packet):
            if purpose == 'recipe-review':
                self.assertNotIn('feature_plan_scope_digest', self.engine.load(key))
                self.assertEqual(self.fx.plan['acceptance_criteria'], packet['plan']['acceptance_criteria'])
                self.assertIn('test_value.py', packet['test_code'])
                reviewed.append(purpose)
            return {'approved': True, 'reason': 'Explicit fake review verdict.'}
        with self.mocks(), patch.object(self.engine, 'judge', side_effect=judge):
            data = self.plan()
        self.assertEqual(['recipe-review'], reviewed)
        self.assertEqual('feature', data['plan']['category'])
        self.assertEqual('REPRODUCE', data['stage'])
        self.assertEqual(data['feature_request']['scope_digest'], data['feature_plan_scope_digest'])
        self.assertNotIn('maintainer_approval', data)

    def test_repair_rechecks_new_assignment_before_edit_model(self):
        with self.mocks():
            data = self.plan()
            self.issue['assignees'] = [{'login': 'other-contributor'}]
            with patch.object(self.engine, 'model') as model:
                with self.assertRaisesRegex(ContributionPaused, 'assigned'):
                    self.engine.repair(self.key, data)
                model.assert_not_called()
        self.assertEqual(2, self.gets.count(('api', 'repos/owner/repo/issues/1')))
        self.assertEqual('VALUE = 1\n', (self.fx.work / 'value.py').read_text(encoding='utf-8'))

    def test_repair_rejects_changed_scope_even_with_prior_plan(self):
        with self.mocks():
            data = self.plan()
            self.issue['body'] = 'Replace authentication and remove compatibility instead.'
            with patch.object(self.engine, 'model') as model:
                with self.assertRaisesRegex(ContributionPaused, 'replan and revalidate'):
                    self.engine.repair(self.key, data)
                model.assert_not_called()

    def test_publish_fresh_check_blocks_before_fork_push_and_pr_creation(self):
        with self.mocks():
            data = self.verified_publication()
            self.issue['assignees'] = [{'login': 'new-owner'}]
            with self.assertRaisesRegex(ContributionPaused, 'assigned'):
                self.engine.publish_pr(self.key, data)
        self.assertEqual([], self.order)
        self.assertEqual([], self.fx.created_prs)
        self.assertEqual([], self.fx.created_issues)
        self.assertEqual('PUBLISH', self.engine.load(self.key)['stage'])

    def test_eligible_publish_checks_fresh_scope_before_local_remote_operations(self):
        actual_check = features.check
        def check(engine, key, data, phase='plan'):
            if phase == 'publish':
                self.order.append(('feature-check', phase))
            return actual_check(engine, key, data, phase)
        with self.mocks(), patch('autonomous.features.check', side_effect=check):
            data = self.verified_publication()
            self.engine.publish_pr(self.key, data)
        self.assertEqual(('feature-check', 'publish'), self.order[0])
        self.assertIn(('gh', 'repo', 'fork'), self.order)
        self.assertTrue(any(op[:2] == ('git', 'push') for op in self.order))
        self.assertIn(('gh', 'pr', 'create'), self.order)
        self.assertEqual(1, len(self.fx.created_prs))
        self.assertEqual([], self.fx.created_issues)
        self.assertEqual('PR_OPEN', self.engine.load(self.key)['stage'])

    def test_publish_reclassified_cached_bug_requires_feature_plan_before_remote_writes(self):
        feature_issue = copy.deepcopy(self.issue)
        self.issue.update(title='Bug: wrong value', body='The existing value is incorrect.',
                          labels=[{'name': 'bug'}])
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET payload=?,title=? WHERE id=?',
                              (json.dumps(self.issue), self.issue['title'], self.key))
        self.fx.plan.pop('acceptance_criteria')
        with self.mocks(), patch.object(self.engine, 'live_issue', side_effect=lambda task: copy.deepcopy(self.issue)):
            data = self.verified_publication()
            self.assertNotIn('feature_request', data)
            self.assertNotIn('feature_plan_scope_digest', data)
            generic_plan = copy.deepcopy(data['plan'])
            self.issue = feature_issue
            with self.assertRaisesRegex(ContributionPaused, 'replan and revalidate'):
                self.engine.publish_pr(self.key, data)
        saved = self.engine.load(self.key)
        self.assertEqual('PUBLISH', saved['stage'])
        self.assertEqual('PUBLISH', self.p.task(self.key)['state'])
        self.assertEqual(generic_plan, saved['plan'])
        self.assertNotIn('feature_plan_scope_digest', saved)
        self.assertTrue(features.feature_task(self.p.config, self.p.task(self.key), saved))
        self.assertEqual([{'name': 'bug'}], json.loads(self.p.task(self.key)['payload'])['labels'])
        self.assertEqual([], self.order)
        self.assertEqual([], self.fx.created_prs)
        self.assertEqual([], self.fx.created_issues)

    def test_feature_lost_pr_response_restores_unique_saved_head_before_admission(self):
        with self.mocks():
            data = self.verified_publication()
            self.fx.lose_pr_response = True
            with self.assertRaisesRegex(RuntimeError, 'Network lost'):
                self.engine.publish_pr(self.key, data)
        restored = self.engine.load(self.key)
        self.assertTrue(restored['pr_create_pending'])
        self.assertNotIn('pr_number', restored)
        head = self.fx.git('rev-parse', 'HEAD')
        self.linked_prs = [{**self.fx.created_prs[0], 'user': {'login': 'bot'},
                           'head': {'ref': restored['branch'], 'sha': head}}]
        self.order.clear()
        with self.mocks(), patch.object(self.engine, 'flat', wraps=self.engine.flat) as lookup, \
                patch('feature_request_flow.collect', side_effect=AssertionError('Recovered PR must not be mistaken for fresh competition')):
            self.engine.publish_pr(self.key, restored)
        expected = f"repos/owner/repo/pulls?head=bot:{restored['branch']}&state=all&per_page=100"
        lookup.assert_any_call(expected)
        self.assertEqual(3, restored['pr_number'])
        self.assertFalse(restored['pr_create_pending'])
        self.assertEqual('PR_OPEN', restored['stage'])
        self.assertEqual(1, len(self.fx.created_prs))
        self.assertEqual(head, self.fx.git('rev-parse', 'HEAD'))
        self.assertFalse(any(op[:2] == ('git', 'push') for op in self.order))
        self.assertNotIn(('gh', 'pr', 'create'), self.order)

    def test_other_own_linked_pr_does_not_bypass_feature_competition_check(self):
        def judge(key, purpose, packet):
            result = {'approved': True, 'reason': 'Explicit fixture review.'}
            if purpose == 'feature-linked-pr-review':
                pr = self.linked_prs[0]
                result['decisions'] = [{'url': pr['html_url'], 'head_sha': pr['head']['sha'],
                    'relation': 'competing', 'reason': 'This other branch implements the requested value.',
                    'evidence': [{'source': 'issue', 'quote': self.issue['body']},
                                 {'source': 'pr', 'quote': pr['body']}]}]
            return result
        with self.mocks(), patch.object(self.engine, 'judge', side_effect=judge):
            data = self.verified_publication()
            self.linked_prs = [{'number': 44, 'state': 'open', 'user': {'login': 'bot'},
                               'html_url': 'https://github.com/owner/repo/pull/44',
                               'base': {'repo': {'full_name': 'owner/repo'}},
                               'title': 'Return the second value', 'body': 'Return the second value in this implementation.',
                               'head': {'ref': 'different-branch', 'sha': 'a' * 40}}]
            with self.assertRaisesRegex(ContributionPaused, 'Existing open linked PR'):
                self.engine.publish_pr(self.key, data)
        self.assertNotIn('pr_number', data)
        self.assertEqual([], self.order)
        self.assertEqual([], self.fx.created_prs)

    def test_existing_pr_maintenance_never_reopens_feature_admission_or_claim(self):
        self.engine.store(self.key, {'stage': 'PR_OPEN', 'attempts': {}, 'pr_number': 3,
                                    'plan': self.fx.plan}, 'PR_OPEN')
        self.p.config['feature_requests']['enabled'] = False
        with self.mocks(), patch('feature_request_flow.collect', side_effect=AssertionError('Must not recollect a published feature')), \
                patch.object(self.engine, 'plan', side_effect=AssertionError('Must not replan')), \
                patch.object(self.engine, 'publish_issue', side_effect=AssertionError('Must not open or claim an issue')):
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
        self.assertEqual([], self.gets)
        self.assertEqual([], self.fx.created_issues)
        self.assertEqual([], self.fx.created_prs)


if __name__ == '__main__':
    unittest.main()
