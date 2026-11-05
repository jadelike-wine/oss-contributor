"""Real temporary SQLite, fake GET transport and explicit fake reviewer verdicts.

No production DB, network, model, repository checkout or publication is used.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autonomous import Autonomous
from contribute import ContributionPaused, Pipeline
import feature_request_flow as flow
from feature_requests import scope_digest
from scheduling import RunTaskBudget
from telemetry import BudgetExhausted


NOW = '2026-11-05T08:00:00+00:00'
BASE = 'repos/owner/repo/issues/12'


def comment(number, body='Useful request.', author='reader', association='NONE'):
    return {'id': number, 'body': body, 'user': {'login': author, 'type': 'User'},
            'author_association': association, 'created_at': '2026-11-05T07:00:00Z',
            'html_url': f'https://github.com/owner/repo/issues/12#issuecomment-{number}'}


def reference(number, repo='owner/repo', old_state='closed'):
    return {'event': 'cross-referenced', 'source': {'issue': {'number': number,
            'state': old_state, 'pull_request': {'url': f'https://api.github.com/repos/{repo}/pulls/{number}'}}}}


class FeatureRequestFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = {'repositories': ['owner/repo'], 'feature_requests': {'enabled': True, 'candidates_per_tick': 2},
                  'repository_checks': {'policies': {}}, 'automation': {'max_stage_attempts': 3}}
        (self.root / 'config.json').write_text(json.dumps(config), encoding='utf-8')
        self.p = Pipeline(self.root)  # External writes stay disabled.
        self.addCleanup(self.p.db.close)
        self.engine = Autonomous(self.p)
        self.key = 'owner--repo--12'
        self.issue = {'number': 12, 'title': 'Feature request: export a document',
                      'body': 'Export one current document.', 'state': 'open',
                      'html_url': 'https://github.com/owner/repo/issues/12',
                      'labels': [{'name': 'enhancement'}], 'assignees': []}
        with self.p.db:
            self.p.db.execute('INSERT INTO tasks(id,repo,number,source,title,url,score,state,holds,payload,updated) '
                              'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                              (self.key, 'owner/repo', 12, 'issue', self.issue['title'], self.issue['html_url'],
                               20, 'DISCOVERED', '[]', json.dumps(self.issue), NOW))
        self.rules = 'Contributions welcome. Follow existing project style and test your patch.'
        self.calls = []
        self.responses = {BASE: self.issue, BASE + '/comments?per_page=100&page=1': [],
                          BASE + '/timeline?per_page=100&page=1': [], 'user': {'login': 'our-user'}}
        self.judge_calls = []
        self.decisions = {}
        self.gh_patch = patch('feature_request_flow.gh', side_effect=self.fake_get)
        self.gh_patch.start()
        self.addCleanup(self.gh_patch.stop)
        clock = patch('feature_request_flow.now', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        policy = patch.object(self.p, 'require_contribution', side_effect=self.refresh_policy)
        self.refresh = policy.start()
        self.addCleanup(policy.stop)
        review = patch.object(self.engine, 'judge', side_effect=self.fake_judge)
        self.judge = review.start()
        self.addCleanup(review.stop)
        # No subprocess/CLI or actual provider call is legitimate in this lane.
        for target in ('contribute.command', 'autonomous.command', 'Autonomous.model'):
            if target == 'Autonomous.model':
                guard = patch.object(Autonomous, 'model', side_effect=AssertionError('Actual model forbidden'))
            else:
                guard = patch(target, side_effect=AssertionError('External command forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def fake_get(self, *args):
        self.calls.append(args)
        self.assertEqual(2, len(args), 'Only a plain GET endpoint is permitted')
        self.assertEqual('api', args[0], 'Issue/PR/comment mutation command is forbidden')
        self.assertIn(args[1], self.responses, 'Unexpected API route')
        result = self.responses[args[1]]
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)

    def refresh_policy(self, task, **kwargs):
        self.assertIs(kwargs.get('refresh'), True)
        self.p._repository_evidence[task['repo']] = ({}, self.rules)

    def fake_judge(self, key, purpose, packet):
        self.assertEqual(self.key, key)
        self.assertIn(purpose, ('feature-scope-review', 'feature-request-review'))
        self.judge_calls.append((purpose, copy.deepcopy(packet)))
        return copy.deepcopy(self.decisions.get(purpose, {'approved': True, 'reason': 'Explicit fake reviewer approval for this isolated test.'}))

    def check(self, phase='plan'):
        return flow.check(self.engine, self.key, self.engine.load(self.key), phase)

    def artifact(self, name):
        return json.loads((self.root / 'runs' / self.key / name).read_text(encoding='utf-8'))

    def hold(self, holds=None, state='HOLD'):
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET state=?,holds=? WHERE id=?',
                              (state, json.dumps(holds or ['feature needs maintainer approval']), self.key))

    def test_default_disabled_and_task_type_guard_do_not_fetch_or_review(self):
        self.p.config.pop('feature_requests')
        with self.assertRaisesRegex(ContributionPaused, 'disabled'):
            self.check()
        self.assertFalse(self.calls)
        self.assertFalse(self.judge_calls)
        self.assertEqual((False, 2), flow.options({}))
        for config in ({'enabled': 'yes'}, {'enabled': True, 'candidates_per_tick': 9}):
            with self.assertRaises(ValueError):
                flow.options({'feature_requests': config})
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?',
                              (json.dumps({**self.issue, 'title': 'Ordinary bug', 'labels': [{'name': 'bug'}]}), self.key))
        self.assertIsNone(self.check(), 'Disabling feature admission must not intercept ordinary bugs')

    def test_collect_fully_paginates_gets_and_persists_complete_snapshot(self):
        self.responses[BASE + '/comments?per_page=100&page=1'] = [comment(i) for i in range(1, 101)]
        self.responses[BASE + '/comments?per_page=100&page=2'] = [comment(101)]
        result = flow.collect(self.engine, self.p.task(self.key))
        self.assertEqual(101, len(result['comments']))
        self.assertTrue(all(result['complete'].values()))
        self.assertEqual('our-user', result['actor'])
        self.assertEqual(result, self.artifact('feature-request-context.json'))
        self.assertIn(('api', BASE + '/comments?per_page=100&page=2'), self.calls)
        self.judge.assert_not_called()

    def test_full_page_cap_remains_unknown_and_does_not_release_legacy_hold(self):
        self.hold()
        for page in range(1, 4):
            self.responses[BASE + f'/comments?per_page=100&page={page}'] = [comment(i) for i in range(page * 100, (page + 1) * 100)]
        with self.assertRaisesRegex(ContributionPaused, 'Incomplete comments'):
            self.check()
        context = self.artifact('feature-request-context.json')
        self.assertEqual(300, len(context['comments']))
        self.assertFalse(context['complete']['comments'])
        self.assertEqual('unknown', self.artifact('feature-request-admission.json')['status'])
        self.assertEqual('HOLD', self.p.task(self.key)['state'])
        self.judge.assert_not_called()

    def test_page_failure_checkpoints_unknown_evidence_and_never_reviews(self):
        self.hold()
        self.responses[BASE + '/comments?per_page=100&page=1'] = [comment(i) for i in range(1, 101)]
        self.responses[BASE + '/comments?per_page=100&page=2'] = RuntimeError('Read-only API unavailable')
        with self.assertRaisesRegex((RuntimeError, ContributionPaused), 'API unavailable'):
            self.check()
        context = self.artifact('feature-request-context.json')
        self.assertEqual(100, len(context['comments']), 'Already fetched evidence must survive a later page error')
        self.assertFalse(context['complete']['comments'])
        self.assertFalse(context['complete']['linked_prs'])
        self.assertEqual('unknown', self.artifact('feature-request-admission.json')['status'])
        self.assertEqual('HOLD', self.p.task(self.key)['state'])
        self.judge.assert_not_called()

    def test_global_budget_exhaustion_is_checkpointed_and_rethrown(self):
        failure = BudgetExhausted('run_model_calls_exhausted')
        self.responses[BASE + '/comments?per_page=100&page=1'] = failure
        with self.assertRaises(BudgetExhausted) as caught:
            self.check()
        self.assertIs(failure, caught.exception)
        self.assertEqual('unknown', self.artifact('feature-request-admission.json')['status'])
        self.assertFalse(self.artifact('feature-request-context.json')['complete']['comments'])
        self.judge.assert_not_called()

    def test_wrong_issue_number_or_repository_cannot_be_admitted(self):
        for change in ({'number': 13, 'html_url': 'https://github.com/owner/repo/issues/13'},
                       {'html_url': 'https://github.com/another/repo/issues/12'}):
            with self.subTest(change=change):
                self.responses[BASE] = {**self.issue, **change}
                with self.assertRaisesRegex(ValueError, 'identity'):
                    self.check()
                self.assertFalse(self.artifact('feature-request-context.json')['complete']['issue'])
                self.assertEqual('unknown', self.artifact('feature-request-admission.json')['status'])
        self.judge.assert_not_called()

    def test_collection_that_outlives_freshness_bound_is_unknown(self):
        self.hold()
        late = '2026-11-05T08:16:00+00:00'
        times = iter([NOW, late])
        with patch('feature_request_flow.now', side_effect=lambda: next(times, late)):
            with self.assertRaisesRegex(ContributionPaused, 'Stale'):
                self.check()
        self.assertEqual(NOW, self.artifact('feature-request-context.json')['observed_at'])
        self.assertEqual('unknown', self.artifact('feature-request-admission.json')['status'])
        self.assertEqual('HOLD', self.p.task(self.key)['state'])
        self.judge.assert_not_called()

    def test_slow_positive_judge_cannot_release_hold_using_expired_snapshot(self):
        self.hold()
        clock = [NOW]
        def slow_fake_judge(key, purpose, packet):
            result = self.fake_judge(key, purpose, packet)
            clock[0] = '2026-11-05T08:16:00+00:00'
            return result
        with patch('feature_request_flow.now', side_effect=lambda: clock[0]), \
                patch.object(self.engine, 'judge', side_effect=slow_fake_judge):
            with self.assertRaisesRegex(ContributionPaused, 'Stale'):
                self.check()
        admission = self.artifact('feature-request-admission.json')
        self.assertEqual('unknown', admission['status'])
        self.assertFalse(admission['eligible'])
        self.assertEqual('HOLD', self.p.task(self.key)['state'])
        self.assertEqual(NOW, self.artifact('feature-request-context.json')['observed_at'])

    def test_linked_pr_is_fetched_currently_instead_of_trusting_timeline_state(self):
        self.responses[BASE + '/timeline?per_page=100&page=1'] = [reference(44, repo='other/fork', old_state='closed')]
        self.responses['repos/other/fork/pulls/44'] = {'number': 44, 'state': 'open',
                                                     'html_url': 'https://github.com/other/fork/pull/44'}
        with self.assertRaisesRegex(ContributionPaused, 'Existing open linked PR'):
            self.check()
        self.assertIn(('api', 'repos/other/fork/pulls/44'), self.calls)
        self.assertEqual('open', self.artifact('feature-request-context.json')['linked_prs'][0]['state'])
        self.judge.assert_not_called()

    def test_more_than_ten_linked_prs_is_bounded_and_unknown(self):
        self.responses[BASE + '/timeline?per_page=100&page=1'] = [reference(i) for i in range(30, 41)]
        for i in range(30, 41):
            self.responses[f'repos/owner/repo/pulls/{i}'] = {'number': i, 'state': 'closed', 'merged': False}
        with self.assertRaisesRegex(ContributionPaused, 'Incomplete linked_prs'):
            self.check()
        self.assertEqual(10, len(self.artifact('feature-request-context.json')['linked_prs']))
        self.judge.assert_not_called()

    def test_malformed_timeline_or_linked_pr_does_not_prove_no_competition(self):
        self.responses[BASE + '/timeline?per_page=100&page=1'] = [{'event': 'cross-referenced'}]
        with self.assertRaisesRegex(ContributionPaused, 'Incomplete linked_prs'):
            self.check()
        self.responses[BASE + '/timeline?per_page=100&page=1'] = [reference(44)]
        self.responses['repos/owner/repo/pulls/44'] = {'number': 45, 'state': 'closed', 'merged': False}
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.check()
        self.assertFalse(self.artifact('feature-request-context.json')['complete']['linked_prs'])

    def test_closed_assigned_claimed_and_required_policy_are_controller_blocks(self):
        cases = [({'state': 'closed'}, [], 'open issue'),
                 ({'assignees': [{'login': 'someone-else'}]}, [], 'assigned'),
                 ({}, [comment(1, 'I am working on this.', author='other')], 'unresolved claim')]
        for change, comments, message in cases:
            with self.subTest(message=message):
                self.responses[BASE] = {**self.issue, **change}
                self.responses[BASE + '/comments?per_page=100&page=1'] = comments
                with self.assertRaisesRegex(ContributionPaused, message):
                    self.check()
        self.responses[BASE] = self.issue
        self.responses[BASE + '/comments?per_page=100&page=1'] = []
        self.rules = 'Please claim an issue before starting work.'
        with self.assertRaisesRegex(ContributionPaused, 'prior claim'):
            self.check()
        self.rules = 'For features: The PR should be linked to the feature request or proposal issue that has been approved by a maintainer.'
        with self.assertRaisesRegex(ContributionPaused, 'current human maintainer approval'):
            self.check()
        self.judge.assert_not_called()

    def test_scope_review_receipt_binds_comment_and_current_title_body(self):
        self.rules = 'For features, you must get maintainer approval before implementation.'
        self.responses[BASE + '/comments?per_page=100&page=1'] = [comment(1, 'Implement this exact export scope.', 'maintainer', 'MEMBER')]
        returned = self.check()
        self.assertEqual(self.issue, {key: value for key, value in returned.items() if key != 'comments_data'})
        self.assertEqual(self.responses[BASE + '/comments?per_page=100&page=1'], returned['comments_data'])
        data = self.engine.load(self.key)
        self.assertEqual([1], data['maintainer_approval']['comment_ids'])
        self.assertIs(data['maintainer_approved'], True)
        self.assertEqual(scope_digest(self.artifact('feature-request-context.json')), data['feature_request_scope_digest'])
        self.assertEqual(['feature-scope-review', 'feature-request-review'], [x[0] for x in self.judge_calls])
        for field in ('title', 'body'):
            with self.subTest(field=field):
                self.responses[BASE] = {**self.issue, field: self.issue[field] + ' Also replace authentication.'}
                self.decisions['feature-scope-review'] = {'approved': False, 'reason': 'Fake reviewer: old comment does not approve the new scope.'}
                with self.assertRaisesRegex(ContributionPaused, 'current human maintainer approval'):
                    self.check()
                self.assertFalse(self.artifact('feature-request-admission.json')['scope_approval_current'])

    def test_changed_maintainer_comment_invalidates_previous_approval(self):
        self.rules = 'For features, you must get maintainer approval before implementation.'
        comments = [comment(1, 'Implement this exact export scope.', 'maintainer', 'MEMBER')]
        self.responses[BASE + '/comments?per_page=100&page=1'] = comments
        self.check()
        comments.append(comment(2, 'Wait for design review.', 'maintainer', 'MEMBER'))
        self.decisions['feature-scope-review'] = {'approved': False, 'reason': 'Fake reviewer: approval was revoked.'}
        with self.assertRaisesRegex(ContributionPaused, 'current human maintainer approval'):
            self.check()
        self.assertFalse(self.artifact('feature-request-admission.json')['scope_approval_current'])

    def test_negative_discussion_model_verdict_blocks_and_is_checkpointed(self):
        self.hold()
        self.responses[BASE + '/comments?per_page=100&page=1'] = [comment(1, 'We do not want this feature.', 'maintainer', 'MEMBER')]
        self.decisions['feature-request-review'] = {'approved': False, 'reason': 'Fake reviewer observed explicit maintainer rejection.'}
        with self.assertRaisesRegex(ContributionPaused, 'explicit maintainer rejection'):
            self.check()
        self.assertEqual('blocked', self.artifact('feature-request-admission.json')['status'])
        self.assertEqual('HOLD', self.p.task(self.key)['state'])
        self.check_again_rejected = self.engine.load(self.key)['feature_request_review']['approved']
        self.assertFalse(self.check_again_rejected)
        with self.assertRaises(ContributionPaused):
            self.check()
        self.assertEqual(1, self.judge.call_count, 'Unchanged rejected discussion should not repeatedly consume a model call')

    def test_label_change_invalidates_previous_positive_admission_review(self):
        self.check()
        self.responses[BASE]['labels'].append({'name': 'needs-discussion'})
        self.decisions['feature-request-review'] = {'approved': False, 'reason': 'Fake reviewer: new label requires discussion.'}
        with self.assertRaisesRegex(ContributionPaused, 'new label requires discussion'):
            self.check()
        self.assertEqual(2, self.judge.call_count)
        self.assertIn({'name': 'needs-discussion'}, self.judge_calls[-1][1]['issue']['labels'])

    def test_linked_pr_change_invalidates_previous_positive_admission_review(self):
        self.check()
        self.responses[BASE + '/timeline?per_page=100&page=1'] = [reference(44)]
        self.responses['repos/owner/repo/pulls/44'] = {
            'number': 44, 'state': 'closed', 'merged': False,
            'html_url': 'https://github.com/owner/repo/pull/44',
            'title': 'Previous implementation', 'body': 'Maintainer declined this direction.', 'head': {'sha': 'abc'}}
        self.decisions['feature-request-review'] = {'approved': False, 'reason': 'Fake reviewer: prior implementation reveals unresolved design.'}
        with self.assertRaisesRegex(ContributionPaused, 'prior implementation reveals unresolved design'):
            self.check()
        self.assertEqual(2, self.judge.call_count)
        self.assertEqual(44, self.judge_calls[-1][1]['linked_prs'][0]['number'])

    def test_planning_wait_requires_human_scope_review_instead_of_reusing_old_admission(self):
        self.check()
        data = self.engine.load(self.key)
        data['upstream_direction_required'] = True
        self.engine.store(self.key, data)
        with self.assertRaisesRegex(ContributionPaused, 'current human maintainer approval'):
            self.check()
        self.assertEqual(1, self.judge.call_count, 'Previous positive admission must not bypass a later plan wait')
        self.responses[BASE + '/comments?per_page=100&page=1'] = [comment(1, 'Please implement the exact requested export scope.', 'maintainer', 'MEMBER')]
        self.check()
        self.assertEqual(['feature-request-review', 'feature-scope-review', 'feature-request-review'],
                         [purpose for purpose, _ in self.judge_calls])
        self.assertTrue(self.engine.load(self.key)['feature_request']['scope_approval_current'])
        self.assertEqual({}, self.p.config['repository_checks']['policies'], 'Temporary direction requirement must not modify repository configuration')

    def test_only_single_legacy_feature_hold_is_released_after_fresh_admission(self):
        self.hold()
        self.check()
        self.assertEqual('DISCOVERED', self.p.task(self.key)['state'])
        self.assertEqual([], json.loads(self.p.task(self.key)['holds']))
        self.assertEqual(1, self.p.db.execute('SELECT count(*) FROM events WHERE task=?', (self.key,)).fetchone()[0])
        self.assertFalse(self.engine.load(self.key)['feature_request']['scope_approval_current'])
        for state, holds in [('HOLD', ['feature needs maintainer approval', 'assigned']),
                             ('HOLD', ['security']), ('FAILED', ['feature needs maintainer approval']),
                             ('REPAIR', ['feature needs maintainer approval'])]:
            with self.subTest(state=state, holds=holds):
                self.hold(holds, state)
                self.check()
                self.assertEqual(state, self.p.task(self.key)['state'])
                self.assertEqual(holds, json.loads(self.p.task(self.key)['holds']))

    def test_publish_rechecks_live_assignment_and_never_sends_public_actions(self):
        self.check()
        data = self.engine.load(self.key)
        data['feature_plan_scope_digest'] = data['feature_request']['scope_digest']
        self.engine.store(self.key, data)
        self.responses[BASE]['assignees'] = [{'login': 'new-contributor'}]
        with self.assertRaisesRegex(ContributionPaused, 'assigned'):
            self.check(phase='publish')
        self.assertEqual(2, self.calls.count(('api', BASE)))
        self.assertEqual(2, self.refresh.call_count)
        self.assertFalse(self.p.allow_external_writes)
        self.assertTrue(all(args[0] == 'api' and len(args) == 2 for args in self.calls))

    def test_existing_feature_stops_when_feature_delivery_is_disabled(self):
        self.check()
        self.p.config['feature_requests']['enabled'] = False
        self.calls.clear()
        with self.assertRaisesRegex(ContributionPaused, 'disabled'):
            self.check()
        self.assertFalse(self.calls)
        self.assertEqual('PLAN', self.engine.load(self.key)['stage'])

    def test_disabled_waiting_feature_preserves_stage_without_gets_or_judge(self):
        self.check()
        data = self.engine.load(self.key)
        data['stage'] = 'WAITING_MAINTAINER'
        data['upstream_direction_required'] = True
        self.engine.store(self.key, data, 'WAITING_MAINTAINER')
        self.p.config['feature_requests']['enabled'] = False
        self.calls.clear()
        self.judge.reset_mock()
        with patch.object(self.engine, 'live_issue', side_effect=AssertionError('Must not fall back to legacy maintainer flow')):
            self.assertFalse(self.engine.wait_maintainer(self.key, self.engine.load(self.key)))
        self.assertEqual('WAITING_MAINTAINER', self.p.task(self.key)['state'])
        self.assertEqual('WAITING_MAINTAINER', self.engine.load(self.key)['stage'])
        self.assertFalse(self.calls)
        self.judge.assert_not_called()

    def test_live_bug_to_feature_reclassification_obeys_disabled_gate_before_prepare(self):
        old = {**self.issue, 'title': 'Ordinary bug', 'labels': [{'name': 'bug'}]}
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(old), self.key))
        self.p.config['feature_requests']['enabled'] = False
        with patch.object(self.engine, 'live_issue', return_value=copy.deepcopy(self.issue)), \
                patch.object(self.p, 'prepare') as prepare, patch.object(self.engine, 'model') as model:
            with self.assertRaisesRegex(ContributionPaused, 'disabled'):
                self.engine.plan(self.key, self.engine.load(self.key))
        prepare.assert_not_called()
        model.assert_not_called()
        self.judge.assert_not_called()
        self.assertEqual('live_issue', self.engine.load(self.key)['feature_request']['detected_from'])
        self.assertEqual('PLAN', self.engine.load(self.key)['stage'])
        self.assertFalse(self.calls)

    def test_live_bug_to_feature_reclassification_requires_new_acceptance_plan_before_repair(self):
        old = {**self.issue, 'title': 'Ordinary bug', 'labels': [{'name': 'bug'}]}
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(old), self.key))
        self.engine.store(self.key, {'stage': 'REPAIR', 'attempts': {}, 'plan': {'category': 'bug'}}, 'REPAIR')
        with patch.object(self.engine, 'live_issue', return_value=copy.deepcopy(self.issue)), \
                patch.object(self.engine, 'model') as model:
            with self.assertRaisesRegex(ContributionPaused, 'replan and revalidate'):
                self.engine.repair(self.key, self.engine.load(self.key))
        model.assert_not_called()
        self.judge.assert_not_called()
        data = self.engine.load(self.key)
        self.assertEqual('REPAIR', data['stage'])
        self.assertNotIn('feature_plan_scope_digest', data)
        self.assertFalse(data['feature_request']['eligible'])

    def test_reclassified_feature_obeys_quota_and_already_admitted_id_is_not_charged_twice(self):
        self.p.config['automation']['implementation_tasks_per_tick'] = 8
        old = {**self.issue, 'title': 'Ordinary bug', 'labels': [{'name': 'bug'}]}
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(old), self.key))
        data = self.engine.load(self.key)
        flow.observe(self.engine, self.p.task(self.key), data, self.issue)
        budget = RunTaskBudget(self.p.config)
        budget.feature_requests = {'owner--repo--10', 'owner--repo--11'}
        for key in budget.feature_requests:
            self.assertTrue(budget.admit(key, 'implementation'))
        self.engine._task_budget = budget
        with self.assertRaisesRegex(ContributionPaused, 'admission budget is exhausted'):
            self.check()
        self.assertFalse(self.calls, 'The third newly recognized feature must stop before GET collection')
        self.judge.assert_not_called()
        self.assertNotIn(self.key, budget.feature_requests)
        self.assertEqual(2, len(budget.used['implementation']))
        # A separate already-admitted scenario is allowed at the same quota;
        # reaching another plan gate must not reserve the same task twice.
        admitted = RunTaskBudget(self.p.config)
        admitted.feature_requests = {self.key, 'owner--repo--11'}
        for key in admitted.feature_requests:
            self.assertTrue(admitted.admit(key, 'implementation'))
        self.engine._task_budget = admitted
        with patch.object(admitted, 'admit', wraps=admitted.admit) as reserve:
            self.check()
        reserve.assert_not_called()
        self.assertEqual(2, len(admitted.feature_requests))
        self.assertEqual(2, len(admitted.used['implementation']))
        self.assertTrue(self.calls)

    def test_repair_and_publication_require_current_acceptance_plan_scope(self):
        self.check()
        for phase in ('repair', 'publish'):
            with self.subTest(phase=phase):
                with self.assertRaisesRegex(ContributionPaused, 'replan and revalidate'):
                    self.check(phase)
        data = self.engine.load(self.key)
        data['feature_plan_scope_digest'] = scope_digest(self.artifact('feature-request-context.json'))
        data['plan'] = {'category': 'feature', 'acceptance_criteria': ['Export only the current document']}
        self.engine.store(self.key, data)
        for phase in ('repair', 'publish'):
            returned = self.check(phase)
            self.assertEqual(self.issue, {key: value for key, value in returned.items() if key != 'comments_data'})
            self.assertEqual([], returned['comments_data'])
        self.responses[BASE] = {**self.issue, 'body': 'Replace authentication instead of export.'}
        for phase in ('repair', 'publish'):
            with self.subTest(changed_phase=phase):
                with self.assertRaisesRegex(ContributionPaused, 'replan and revalidate'):
                    self.check(phase)
        self.assertEqual(2, self.judge.call_count, 'First repair reviews the plan; changed issue scope must then pause before another review')

    def test_current_acceptance_plan_is_reviewed_against_complete_discussion(self):
        comments = [comment(1, 'Export only the current document; do not add bulk export.', 'maintainer', 'MEMBER')]
        self.responses[BASE + '/comments?per_page=100&page=1'] = comments
        self.check()
        data = self.engine.load(self.key)
        data['feature_plan_scope_digest'] = data['feature_request']['scope_digest']
        data['plan'] = {'category': 'feature', 'acceptance_criteria': ['Export only the current document'],
                        'spec': {'test_files': ['test_export.py']}}
        self.engine.store(self.key, data)
        self.check('repair')
        packet = self.judge_calls[-1][1]
        self.assertEqual(data['plan'], packet['acceptance_plan'])
        self.assertEqual(comments, packet['comments'])
        self.assertEqual(2, self.judge.call_count, 'A plan needs review beyond initial request admission')
        data = self.engine.load(self.key)
        data['plan']['acceptance_criteria'] = ['Export every document in the organization']
        self.engine.store(self.key, data)
        self.decisions['feature-request-review'] = {'approved': False, 'reason': 'Fake reviewer: acceptance plan exceeds the current discussion.'}
        with self.assertRaisesRegex(ContributionPaused, 'acceptance plan exceeds'):
            self.check('publish')
        self.assertEqual(3, self.judge.call_count)
        self.assertEqual(data['plan'], self.judge_calls[-1][1]['acceptance_plan'])

    def test_planning_model_and_recipe_review_receive_current_scope_comments(self):
        comments = [comment(1, 'Export only the current document; never all documents.', 'maintainer', 'MEMBER')]
        self.responses[BASE + '/comments?per_page=100&page=1'] = comments
        work = self.root / 'fixture'
        work.mkdir()
        (work / 'test_export.py').write_text('assert False, "CURRENT_EXPORT_MISSING"\n', encoding='utf-8')
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET workspace=?,base_sha=? WHERE id=?', (str(work), 'test-base', self.key))
        plan = {'decision': 'ready', 'reason': 'Fake plan for context routing only.', 'title': 'Export current document',
                'issue_body': 'Existing issue.', 'pr_title': 'feat: export current document', 'pr_body': 'Targeted export.',
                'acceptance_criteria': ['Export only the current document'], 'duplicate_queries': ['current export'],
                'spec': {'setup': [], 'regression': ['python', 'test_export.py'], 'checks': [['python', 'test_export.py']],
                         'test_files': ['test_export.py'], 'baseline_failure_pattern': 'CURRENT_EXPORT_MISSING'}}
        prompts, recipes = [], []
        def fake_model(key, purpose, prompt, edit=False):
            self.assertEqual('plan', purpose)
            prompts.append(prompt)
            return copy.deepcopy(plan)
        def fake_recipe_or_admission(key, purpose, packet):
            if purpose == 'recipe-review':
                recipes.append(copy.deepcopy(packet))
                return {'approved': True, 'reason': 'Fake recipe review, no real validation claimed.'}
            return self.fake_judge(key, purpose, packet)
        with patch.object(self.p, 'prepare'), patch.object(self.engine, 'planning_baseline', return_value={}), \
                patch.object(self.engine, 'own_contribution', return_value=False), \
                patch.object(self.engine, 'model', side_effect=fake_model), \
                patch.object(self.engine, 'judge', side_effect=fake_recipe_or_admission), \
                patch('autonomous.runtime_tools', return_value={}), \
                patch('autonomous.changed_files', return_value=['test_export.py']):
            self.engine.plan(self.key, self.engine.load(self.key))
        self.assertEqual(1, len(prompts))
        self.assertIn('"comments_data"', prompts[0])
        self.assertIn(comments[0]['body'], prompts[0])
        self.assertEqual(comments, recipes[0]['issue']['comments_data'])
        self.assertEqual('feature', self.engine.load(self.key)['plan']['category'])
        self.assertEqual('REPRODUCE', self.engine.load(self.key)['stage'])


if __name__ == '__main__':
    unittest.main()
