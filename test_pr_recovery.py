"""Real local Git recovery with fake GitHub; no network or public writes."""
import copy
import contextlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_autonomous as fixtures
from contribute import ContributionPaused, command as real_command
from delivery import ensure_commit
from pr_maintenance import register_existing_pr, prepare_merge, finish_merge_resolution, queue_conflict
from telemetry import maintenance_gaps, observe_pr


class PRRecoveryTests(unittest.TestCase):
    git = fixtures.AutonomousTests.git
    model = fixtures.AutonomousTests.model
    tearDown = fixtures.AutonomousTests.tearDown

    def setUp(self):
        fixtures.AutonomousTests.setUp(self)
        self.git('config', 'core.autocrlf', 'false')
        (self.work / 'value.py').write_text('VALUE = 3\n', encoding='utf-8')
        self.git('commit', '-qam', 'upstream changes the same line')
        self.upstream = self.git('rev-parse', 'HEAD')
        self.target = self.upstream
        self.git('checkout', '-qb', 'codex/bug', self.base)
        self.model(self.key, 'plan', '')
        self.model(self.key, 'repair', '')
        self.git('add', '.')
        self.git('commit', '-qm', 'published fix')
        self.head = self.git('rev-parse', 'HEAD')
        self.git('remote', 'add', 'origin', 'https://github.com/bot/repo.git')
        self.pr = {'number': 42, 'state': 'open', 'title': 'fix: value', 'body': 'Fixes #1',
                   'html_url': 'https://github.com/owner/repo/pull/42',
                   'user': {'login': 'bot'},
                   'head': {'sha': self.head, 'ref': 'codex/bug',
                            'repo': {'full_name': 'bot/repo', 'owner': {'login': 'bot'}}},
                   'base': {'sha': self.upstream, 'ref': 'main', 'repo': {'full_name': 'owner/repo'}}}
        self.view = {'number': 42, 'state': 'OPEN', 'mergedAt': None, 'body': 'Fixes #1',
                     'headRefOid': self.head, 'headRefName': 'codex/bug',
                     'baseRefOid': self.upstream, 'baseRefName': 'main',
                     'targetBaseOid': self.target,
                     'mergeable': 'CONFLICTING', 'mergeStateStatus': 'DIRTY',
                     'reviewDecision': '', 'statusCheckRollup': []}
        self.data = {'stage': 'PR_OPEN', 'attempts': {}, 'pr_number': 42,
                     'pr_url': self.pr['html_url'], 'branch': 'codex/bug', 'plan': self.plan}
        self.calls = []
        self.remote_calls = []
        self.engine.store(self.key, self.data, 'PR_OPEN')

    def fake_gh(self, *args):
        self.remote_calls.append(args)
        self.assertNotIn('-X', args, 'All GitHub calls in recovery must be reads')
        self.assertNotIn('edit', args)
        endpoint = args[-1]
        if args[0] == 'pr':
            return copy.deepcopy(self.view)
        if endpoint == 'user':
            return {'login': 'bot'}
        if endpoint == 'repos/owner/repo/pulls/42':
            return copy.deepcopy(self.pr)
        if endpoint == 'repos/owner/repo/git/ref/heads/main':
            return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': self.target}}
        if endpoint == 'repos/bot/repo':
            return {'full_name': 'bot/repo', 'fork': True, 'owner': {'login': 'bot'},
                    'parent': {'full_name': 'owner/repo'}, 'clone_url': 'https://github.com/bot/repo.git'}
        if '--slurp' in args:
            return [self.comments]
        raise AssertionError('Unexpected GitHub read: ' + str(args))

    def local_command(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.assertNotEqual('gh', argv[0], 'No actual gh subprocess is allowed')
        self.assertNotIn('push', argv, 'Recovery tests never push')
        if argv[:2] == ['git', 'fetch']:
            argv = [str(self.work) if x == 'https://github.com/owner/repo.git' else x for x in argv]
        return real_command(argv, **kwargs)

    def mocks(self):
        stack = contextlib.ExitStack()
        stack.enter_context(patch('contribute.gh', side_effect=self.fake_gh))
        stack.enter_context(patch('autonomous.gh', side_effect=self.fake_gh))
        stack.enter_context(patch('contribute.command', side_effect=self.local_command))
        stack.enter_context(patch('autonomous.command', side_effect=self.local_command))
        stack.enter_context(patch.object(self.engine, 'sync_issue_link'))
        stack.enter_context(patch.object(self.engine, 'pr_template', return_value=''))
        stack.enter_context(patch('autonomous.observe_ci', return_value={
            'state': 'approval_required', 'failed_checks': []}))
        return stack

    def register(self, **kwargs):
        return register_existing_pr(self.p, 'owner/repo', 42, self.work, self.base,
                                    expected_head=self.head, expected_base=self.upstream, **kwargs)

    def unlink(self):
        with self.p.db:
            self.p.db.execute('DELETE FROM automation WHERE task=?', (self.key,))

    def test_preview_has_no_database_or_git_content_changes(self):
        self.unlink()
        before = list(self.p.db.iterdump())
        with self.mocks():
            result = self.register()
        self.assertFalse(result['applied'])
        self.assertEqual(before, list(self.p.db.iterdump()))
        self.assertEqual(self.head, self.git('rev-parse', 'HEAD'))
        self.assertEqual('', self.git('status', '--porcelain'))

    def test_clean_adoption_does_not_materialize_or_write_a_content_tree(self):
        self.unlink()
        with self.mocks(), patch('delivery.content_tree', side_effect=AssertionError('must not hydrate partial clone')):
            result = self.register(apply=True)
        self.assertTrue(result['applied'])
        self.assertEqual('tree-v2:' + self.git('rev-parse', 'HEAD^{tree}'), result['receipt']['content_fingerprint'])
        self.assertFalse(any(argv[:2] in (['git', 'read-tree'], ['git', 'write-tree'], ['git', 'add'])
                             for argv in self.calls))

    def test_hidden_index_flags_are_rejected_without_clearing_flags_or_changes(self):
        self.unlink()
        before = list(self.p.db.iterdump())
        for flag in ('assume-unchanged', 'skip-worktree'):
            with self.subTest(flag=flag):
                self.git('update-index', '--' + flag, 'value.py')
                (self.work / 'value.py').write_text('HIDDEN_USER_CHANGE = True\n')
                tag = self.git('ls-files', '-v', 'value.py')
                with self.mocks(), self.assertRaisesRegex(ContributionPaused, 'hidden local changes'):
                    self.register(apply=True)
                self.assertEqual(tag, self.git('ls-files', '-v', 'value.py'))
                self.assertEqual('HIDDEN_USER_CHANGE = True\n', (self.work / 'value.py').read_text())
                self.git('update-index', '--no-' + flag, 'value.py')
                self.git('checkout', '--', 'value.py')
        self.assertEqual(before, list(self.p.db.iterdump()))

    def test_configured_sparse_checkout_registers_without_materializing_omitted_files(self):
        self.unlink()
        self.git('sparse-checkout', 'set', '--no-cone', '/test_value.py')
        self.assertFalse((self.work / 'value.py').exists())
        self.assertTrue(self.git('ls-files', '-v', 'value.py').startswith('S '))
        before_flags = self.git('ls-files', '-v')
        with self.mocks(), patch('delivery.content_tree', side_effect=AssertionError('must not hydrate partial clone')):
            result = self.register(apply=True)
        self.assertTrue(result['applied'])
        self.assertFalse((self.work / 'value.py').exists())
        self.assertEqual(before_flags, self.git('ls-files', '-v'))
        self.assertEqual('true', self.git('config', '--bool', 'core.sparseCheckout'))

    def test_sparse_checkout_rejects_existing_hidden_modified_file(self):
        self.unlink()
        self.git('sparse-checkout', 'set', '--no-cone', '/test_value.py')
        (self.work / 'value.py').write_text('HIDDEN_SPARSE_CHANGE = True\n')
        before_flags = self.git('-c', 'core.sparseCheckout=false', 'ls-files', '-v')
        self.assertIn('S value.py', before_flags)
        before_db = list(self.p.db.iterdump())
        with self.mocks(), self.assertRaisesRegex(ContributionPaused, 'hidden local changes'):
            self.register(apply=True)
        self.assertEqual(before_db, list(self.p.db.iterdump()))
        self.assertEqual(before_flags, self.git('-c', 'core.sparseCheckout=false', 'ls-files', '-v'))
        self.assertEqual('HIDDEN_SPARSE_CHANGE = True\n', (self.work / 'value.py').read_text())

    def test_absent_skip_worktree_path_without_sparse_configuration_is_rejected(self):
        self.unlink()
        self.git('update-index', '--skip-worktree', 'value.py')
        (self.work / 'value.py').unlink()
        with self.mocks(), self.assertRaisesRegex(ContributionPaused, 'configured sparse'):
            self.register(apply=True)
        self.assertFalse((self.work / 'value.py').exists())
        self.assertTrue(self.git('ls-files', '-v', 'value.py').startswith('S '))

    def test_new_registration_preserves_failed_task_and_requires_recipe(self):
        self.unlink()
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET state='FAILED' WHERE id=?", (self.key,))
        old = self.p.task(self.key)
        with self.mocks():
            result = self.register(apply=True)
        self.assertEqual(old, self.p.task(self.key))
        task = self.p.task(result['task_id'])
        self.assertEqual('pr_maintenance', task['source'])
        self.assertIsNone(task['number'], 'A PR number must not become an issue identity')
        saved = self.engine.load(task['id'])
        self.assertTrue(saved['maintenance_observation_only'])
        self.assertNotIn('approved_fingerprint', saved)
        self.assertNotIn('plan', saved)

    def test_adoption_rejects_wrong_owner_stale_base_and_dirty_workspace(self):
        self.unlink()
        before = list(self.p.db.iterdump())
        with self.mocks():
            self.pr['user']['login'] = 'someone-else'
            with self.assertRaisesRegex(ContributionPaused, 'owner'):
                self.register(apply=True)
            self.pr['user']['login'] = 'bot'
            self.pr['base']['sha'] = '0' * 40
            with self.assertRaisesRegex(ContributionPaused, 'current head/base'):
                self.register(apply=True)
            self.pr['base']['sha'] = self.upstream
            (self.work / 'value.py').write_text('UNSAVED = True\n')
            with self.assertRaisesRegex(ContributionPaused, 'clean workspace'):
                self.register(apply=True)
        self.assertEqual(before, list(self.p.db.iterdump()))
        self.assertEqual('UNSAVED = True\n', (self.work / 'value.py').read_text())

    def test_registration_never_creates_second_link_to_same_pr(self):
        before = list(self.p.db.iterdump())
        with self.mocks(), self.assertRaisesRegex(ContributionPaused, 'already linked'):
            self.register(apply=True)
        self.assertEqual(before, list(self.p.db.iterdump()))

    def test_stale_pr_base_snapshot_does_not_replace_live_target_tip(self):
        self.pr['base']['sha'] = self.base
        self.view['baseRefOid'] = self.base
        with self.mocks():
            self.engine.feedback(self.key, self.data)
            self.assertEqual(self.base, self.data['pr_merge']['identity']['baseRefOid'])
            self.assertEqual(self.upstream, self.data['pr_merge']['identity']['targetBaseOid'])
            prepare_merge(self.engine, self.key, self.data)
        self.assertEqual(self.upstream, self.p.task(self.key)['base_sha'])
        self.assertIn('<<<<<<<', (self.work / 'value.py').read_text())

    def test_registration_preserves_snapshot_and_checks_target_tip_again_before_apply(self):
        self.unlink()
        self.pr['base']['sha'] = self.base
        def changed_target(*args):
            result = self.fake_gh(*args)
            if args[-1] == 'repos/owner/repo/git/ref/heads/main':
                self.target = 'a' * 40
            return result
        before = list(self.p.db.iterdump())
        with self.mocks():
            result = register_existing_pr(self.p, 'owner/repo', 42, self.work, self.base,
                expected_head=self.head, expected_base=self.base)
            self.assertEqual(self.base, result['receipt']['base_sha'])
            self.assertEqual(self.upstream, result['receipt']['target_base_sha'])
            with patch('contribute.gh', side_effect=changed_target), self.assertRaisesRegex(ContributionPaused, 'changed during PR registration'):
                register_existing_pr(self.p, 'owner/repo', 42, self.work, self.base,
                    expected_head=self.head, expected_base=self.base, apply=True)
        self.assertEqual(before, list(self.p.db.iterdump()))

    def test_snapshot_refresh_during_merge_preserves_exact_target_and_resolution(self):
        self.pr['base']['sha'] = self.base
        self.view['baseRefOid'] = self.base
        with self.mocks():
            self.engine.feedback(self.key, self.data)
            prepare_merge(self.engine, self.key, self.data)
            content = (self.work / 'value.py').read_text()
            self.pr['base']['sha'] = self.target
            prepare_merge(self.engine, self.key, self.data)
            refreshed = {**self.view, 'baseRefOid': self.target}
            self.assertFalse(queue_conflict(self.engine, self.key, self.data, refreshed))
        self.assertEqual(content, (self.work / 'value.py').read_text())
        self.assertEqual('merging', self.data['pr_merge']['state'])
        self.assertEqual(self.target, self.data['pr_merge']['identity']['targetBaseOid'])
        self.assertEqual(self.base, self.data['pr_merge']['metadata_history'][0]['identity']['baseRefOid'])
        self.assertEqual(1, sum(x[:2] == ['git', 'fetch'] for x in self.calls))

    def test_target_advance_before_merge_refreshes_queue_without_editing(self):
        with self.mocks():
            self.engine.feedback(self.key, self.data)
            old = copy.deepcopy(self.data['pr_merge'])
            self.target = 'b' * 40
            with self.assertRaisesRegex(ContributionPaused, 'refreshed exact maintenance identity'):
                prepare_merge(self.engine, self.key, self.data)
        self.assertEqual(self.target, self.data['pr_merge']['identity']['targetBaseOid'])
        self.assertEqual(old, self.data['pr_merge_history'][-1])
        self.assertEqual(self.head, self.git('rev-parse', 'HEAD'))
        self.assertEqual('', self.git('status', '--porcelain'))
        self.assertFalse(any(argv[:2] == ['git', 'fetch'] for argv in self.calls))

    def test_target_advance_during_local_merge_preserves_resolution_for_reconciliation(self):
        with self.mocks():
            self.engine.feedback(self.key, self.data)
            prepare_merge(self.engine, self.key, self.data)
            content = (self.work / 'value.py').read_text()
            self.target = 'c' * 40
            with self.assertRaisesRegex(ContributionPaused, 'local merge was preserved'):
                prepare_merge(self.engine, self.key, self.data)
            with self.assertRaisesRegex(ContributionPaused, 'reconcile'):
                prepare_merge(self.engine, self.key, self.data)
        self.assertEqual(content, (self.work / 'value.py').read_text())
        self.assertEqual('target_changed', self.data['pr_merge']['state'])

    def test_explicit_existing_task_adoption_keeps_previous_record(self):
        self.unlink()
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET state='DISCOVERED' WHERE id=?", (self.key,))
        original = self.p.task(self.key)
        with self.mocks():
            result = self.register(task_id=self.key, issue_number=1, plan={'spec': self.spec}, apply=True)
        saved = self.engine.load(result['task_id'])
        self.assertFalse(saved['maintenance_observation_only'])
        self.assertEqual(original, saved['pr_registration_history'][0]['task'])
        self.assertEqual(self.pr['body'], saved['plan']['pr_body'])
        self.assertNotIn('approved_fingerprint', saved)

    def test_repeated_registration_is_idempotent(self):
        self.unlink()
        with self.mocks():
            first = self.register(plan={'spec': self.spec}, apply=True)
            before = list(self.p.db.iterdump())
            second = self.register(plan={'spec': self.spec}, apply=True)
        self.assertEqual(first['task_id'], second['task_id'])
        self.assertTrue(second['already_registered'])
        self.assertEqual(before, list(self.p.db.iterdump()))

    def test_conflict_queues_without_model_and_same_identity_does_not_duplicate_event(self):
        with self.mocks(), patch.object(self.engine, 'model') as model:
            self.assertTrue(self.engine.feedback(self.key, self.data))
            count = self.p.db.execute('SELECT count(*) FROM events').fetchone()[0]
            queue_conflict(self.engine, self.key, self.data, self.view)
        self.assertEqual('REPAIR', self.data['stage'])
        self.assertEqual('pending', self.data['pr_merge']['state'])
        self.assertEqual(count, self.p.db.execute('SELECT count(*) FROM events').fetchone()[0])
        self.assertEqual(self.head, self.git('rev-parse', 'HEAD'))
        model.assert_not_called()

    def test_action_required_alone_does_not_request_repair(self):
        self.view.update(mergeable='MERGEABLE', mergeStateStatus='BLOCKED')
        with self.mocks(), patch.object(self.engine, 'model') as model:
            self.assertFalse(self.engine.feedback(self.key, self.data))
        self.assertEqual('PR_OPEN', self.data['stage'])
        self.assertNotIn('pr_merge', self.data)
        model.assert_not_called()

    def test_conflict_is_queued_before_unavailable_comments_or_ci_without_inventing_observations(self):
        observe_pr(self.p.db, 'owner/repo', {**self.view, 'ci': {'state': 'approval_required'},
                   'comments': [{'body': 'Review later', 'author_association': 'MEMBER',
                                 'user': {'type': 'User', 'login': 'maintainer'}}]})
        before_ci = dict(self.p.db.execute('SELECT * FROM pr_observations').fetchone())
        before_acceptance = dict(self.p.db.execute('SELECT * FROM pr_acceptance_observations').fetchone())
        with self.mocks(), \
                patch.object(self.engine, 'flat', side_effect=RuntimeError('comments EOF')) as comments, \
                patch('autonomous.observe_ci', side_effect=RuntimeError('CI unavailable')) as ci, \
                patch.object(self.engine, 'model') as model:
            self.assertTrue(self.engine.feedback(self.key, self.data))
        comments.assert_not_called()
        ci.assert_not_called()
        model.assert_not_called()
        self.assertEqual('REPAIR', self.data['stage'])
        self.assertEqual(before_ci, dict(self.p.db.execute('SELECT * FROM pr_observations').fetchone()))
        self.assertEqual(before_acceptance, dict(self.p.db.execute('SELECT * FROM pr_acceptance_observations').fetchone()))
        observed = dict(self.p.db.execute('SELECT * FROM pr_maintenance_observations').fetchone())
        self.assertEqual((self.head, self.upstream, 'CONFLICTING'),
                         (observed['head_sha'], observed['base_sha'], observed['mergeable']))
        report = maintenance_gaps(self.p.db)
        self.assertEqual('merge_conflict', report['pending_actions'][0]['reason'])
        self.assertEqual(self.upstream, report['pending_actions'][0]['identity']['baseRefOid'])
        self.assertFalse((self.root / 'runs' / self.key / 'pr-ci.json').exists())

    def test_nonconflicting_comment_failure_remains_unknown_and_does_not_request_repairs(self):
        self.view.update(mergeable='MERGEABLE', mergeStateStatus='CLEAN')
        with self.mocks(), patch.object(self.engine, 'flat', side_effect=RuntimeError('comments EOF')), \
                patch('autonomous.observe_ci') as ci, patch.object(self.engine, 'model') as model:
            with self.assertRaisesRegex(RuntimeError, 'comments EOF'):
                self.engine.feedback(self.key, self.data)
        self.assertEqual('PR_OPEN', self.data['stage'])
        self.assertEqual(0, self.p.db.execute('SELECT count(*) FROM pr_observations').fetchone()[0])
        ci.assert_not_called()
        model.assert_not_called()

    def test_changed_base_reconsiders_existing_feedback(self):
        self.view.update(mergeable='MERGEABLE', mergeStateStatus='CLEAN')
        self.comments = [{'body': 'Check compatibility with current upstream'}]
        with self.mocks(), patch.object(self.engine, 'model', return_value={'needs_changes': False}) as model:
            self.assertFalse(self.engine.feedback(self.key, self.data))
            self.assertFalse(self.engine.feedback(self.key, self.data))
            self.view['baseRefOid'] = 'a' * 40
            self.assertFalse(self.engine.feedback(self.key, self.data))
        self.assertEqual(2, model.call_count)

    def test_merge_resumes_once_rejects_markers_then_revalidates_exact_new_base(self):
        with self.mocks():
            self.engine.feedback(self.key, self.data)
            prepare_merge(self.engine, self.key, self.data)
            self.assertIn('<<<<<<<', (self.work / 'value.py').read_text())
            self.assertEqual(self.upstream, self.p.task(self.key)['base_sha'])
            prepare_merge(self.engine, self.key, self.engine.load(self.key))
            self.assertEqual(1, sum(x[:2] == ['git', 'fetch'] for x in self.calls))
            with self.assertRaisesRegex(RuntimeError, 'conflict markers'):
                finish_merge_resolution(self.engine, self.key, self.data)
            with patch.object(self.engine, 'model', side_effect=self.model):
                self.engine.repair(self.key, self.data)
            self.assertEqual('PATCH_READY', self.data['stage'])
            self.assertEqual('resolved_unverified', self.data['pr_merge']['state'])
            self.assertNotIn('approved_fingerprint', self.data)
            self.engine.verify(self.key, self.data)
            with patch.object(self.engine, 'judge', return_value={'approved': True, 'reason': 'tested'}):
                self.engine.review(self.key, self.data)
            evidence = json.loads((self.root / 'runs' / self.key / 'verification.json').read_text())
            self.assertEqual(self.upstream, evidence['base_sha'])
            self.assertEqual([1, 0, 0], [r['exit_code'] for r in evidence['results']])
            ensure_commit(self.work, self.data, 'merge reviewed fix', self.local_command,
                          lambda data: self.engine.store(self.key, data))
        parents = self.git('rev-list', '--parents', '-n', '1', 'HEAD').split()[1:]
        self.assertEqual([self.head, self.upstream], parents)
        self.assertEqual('', self.git('status', '--porcelain'))

    def test_pending_merge_preserves_unrelated_local_changes(self):
        with self.mocks():
            self.engine.feedback(self.key, self.data)
            (self.work / 'value.py').write_text('unsaved user change\n')
            with self.assertRaisesRegex(ContributionPaused, 'clean workspace'):
                prepare_merge(self.engine, self.key, self.data)
        self.assertEqual(self.base, self.p.task(self.key)['base_sha'])
        self.assertEqual('unsaved user change\n', (self.work / 'value.py').read_text())

    def test_publish_merge_recovers_lost_commit_response_without_recommit(self):
        with self.mocks():
            self.engine.feedback(self.key, self.data)
            with patch.object(self.engine, 'model', side_effect=self.model):
                self.engine.repair(self.key, self.data)
            self.engine.verify(self.key, self.data)
            with patch.object(self.engine, 'judge', return_value={'approved': True, 'reason': 'tested'}):
                self.engine.review(self.key, self.data)
            self.data['publication_fork'] = {'full_name': 'bot/repo'}
            self.engine.store(self.key, self.data)
            commits = []
            def lost_response(argv, **kwargs):
                result = self.local_command(argv, **kwargs)
                if argv[:2] == ['git', 'commit']:
                    commits.append(self.git('rev-parse', 'HEAD'))
                    raise RuntimeError('commit response lost')
                return result
            with patch.object(self.p, 'require_contribution'), \
                    patch.object(self.engine, 'live_issue', return_value=None), \
                    patch.object(self.engine, 'flat', return_value=[self.pr]), \
                    patch('delivery.windows_hook_environment', return_value={}), \
                    patch('delivery.ensure_push') as push:
                with patch('autonomous.command', side_effect=lost_response):
                    with self.assertRaisesRegex(RuntimeError, 'commit response lost'):
                        self.engine.publish_pr(self.key, self.data)
                push.assert_not_called()
                current = self.git('rev-parse', 'HEAD')
                restored = self.engine.load(self.key)
                self.engine.publish_pr(self.key, restored)
                self.assertEqual('PR_OPEN', restored['stage'])
                self.assertEqual('committed', restored['pr_merge']['state'])
                self.assertEqual(current, self.git('rev-parse', 'HEAD'))
                push.assert_called_once()
        self.assertEqual([self.head, self.upstream], self.git('show', '-s', '--format=%P', 'HEAD').split())
        self.assertEqual([current], commits)

    def prepare_reviewed_merge(self):
        self.engine.feedback(self.key, self.data)
        with patch.object(self.engine, 'model', side_effect=self.model):
            self.engine.repair(self.key, self.data)
        self.engine.verify(self.key, self.data)
        with patch.object(self.engine, 'judge', return_value={'approved': True, 'reason': 'tested'}):
            self.engine.review(self.key, self.data)
        self.data['publication_fork'] = {'full_name': 'bot/repo'}
        self.engine.store(self.key, self.data)

    def publication_mocks(self):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(self.p, 'require_contribution'))
        stack.enter_context(patch.object(self.engine, 'live_issue', return_value=None))
        stack.enter_context(patch.object(self.engine, 'flat', return_value=[self.pr]))
        stack.enter_context(patch('delivery.windows_hook_environment', return_value={}))
        return stack

    def test_publication_rechecks_live_target_before_commit_despite_stale_pr_snapshot(self):
        with self.mocks():
            self.prepare_reviewed_merge()
            self.target = 'e' * 40
            with self.publication_mocks(), patch('delivery.ensure_push') as push, \
                    self.assertRaisesRegex(ContributionPaused, 'Current target branch changed'):
                self.engine.publish_pr(self.key, self.data)
            push.assert_not_called()
        self.assertEqual(self.head, self.git('rev-parse', 'HEAD'))
        self.assertEqual('resolved_unverified', self.data['pr_merge']['state'])
        self.assertEqual('publication_identity_changed', self.data['maintenance_pending']['reason'])
        self.assertEqual(self.target, self.data['maintenance_pending']['observed_identity']['targetBaseOid'])
        self.assertEqual(self.upstream, self.data['maintenance_pending']['observed_identity']['baseRefOid'])

    def test_target_advance_after_commit_preserves_commit_and_prevents_push(self):
        with self.mocks():
            self.prepare_reviewed_merge()
            def advance_after_commit(*args, **kwargs):
                ensure_commit(*args, **kwargs)
                self.target = 'f' * 40
            with self.publication_mocks(), patch('delivery.ensure_commit', side_effect=advance_after_commit), \
                    patch('delivery.ensure_push') as push, \
                    self.assertRaisesRegex(ContributionPaused, 'Current target branch changed'):
                self.engine.publish_pr(self.key, self.data)
            push.assert_not_called()
        current = self.git('rev-parse', 'HEAD')
        self.assertEqual(current, self.data['publish_commit'])
        self.assertEqual('committed', self.data['pr_merge']['state'])
        self.assertEqual([self.head, self.upstream], self.git('show', '-s', '--format=%P', 'HEAD').split())
        self.assertEqual('publication_identity_changed', maintenance_gaps(self.p.db)['pending_actions'][0]['reason'])

    def test_publication_rejects_changed_remote_head(self):
        with self.mocks():
            self.prepare_reviewed_merge()
            self.pr['head']['sha'] = 'a' * 40
            with self.publication_mocks(), patch('delivery.ensure_push') as push, \
                    self.assertRaisesRegex(ContributionPaused, 'PR head changed'):
                self.engine.publish_pr(self.key, self.data)
            push.assert_not_called()
        self.assertEqual(self.head, self.git('rev-parse', 'HEAD'))

    def test_three_verified_publications_retain_current_head_after_reviews_archive_commit_intents(self):
        published = []
        def publish_locally(work, remote, branch, data, command, persist, **kwargs):
            # Match ensure_push's confirmed readback receipt without a network write.
            self.pr['head']['sha'] = data['publish_commit']
            data['push_verified'] = {'ref': 'refs/heads/' + branch, 'commit': data['publish_commit']}
            published.append(data['publish_commit'])
            persist(data)
        with self.mocks(), self.publication_mocks(), patch('delivery.ensure_push', side_effect=publish_locally):
            self.prepare_reviewed_merge()
            self.engine.publish_pr(self.key, self.data)
            for revision in (2, 3):
                def repair(*args, **kwargs):
                    (self.work / 'value.py').write_text(f'VALUE = 2  # reviewer revision {revision}\n', encoding='utf-8')
                    return {'summary': 'address reviewer detail'}
                self.engine.move(self.key, self.data, 'REPAIR')
                with patch.object(self.engine, 'model', side_effect=repair):
                    self.engine.repair(self.key, self.data)
                self.engine.verify(self.key, self.data)
                with patch.object(self.engine, 'judge', return_value={'approved': True, 'reason': 'tested'}):
                    self.engine.review(self.key, self.data)
                self.assertNotIn('publish_commit', self.data)
                self.engine.publish_pr(self.key, self.data)
                self.assertEqual('PR_OPEN', self.data['stage'])
        self.assertEqual(3, len(set(published)))
        self.assertEqual(published[-1], self.git('rev-parse', 'HEAD'))
        self.assertEqual(published[-1], self.data['push_verified']['commit'])
        self.assertEqual(published[0], self.data['pr_merge']['commit'])

    def test_observation_only_conflict_is_reported_without_edits(self):
        self.data['maintenance_observation_only'] = True
        with self.mocks(), patch.object(self.engine, 'model') as model:
            self.assertFalse(self.engine.feedback(self.key, self.data))
        self.assertEqual('PR_OPEN', self.data['stage'])
        self.assertEqual('needs_verification_recipe', self.data['pr_merge']['state'])
        report = maintenance_gaps(self.p.db)
        self.assertEqual('merge_conflict', report['pending_actions'][0]['reason'])
        self.assertEqual(self.head, self.git('rev-parse', 'HEAD'))
        model.assert_not_called()

    def test_conflict_reported_even_before_task_dispatch(self):
        observe_pr(self.p.db, 'owner/repo', {**self.view, 'ci': {'state': 'approval_required'}})
        report = maintenance_gaps(self.p.db)
        self.assertEqual(0, report['gap_count'])
        self.assertEqual(1, report['pending_action_count'])
        self.assertEqual(self.upstream, report['pending_actions'][0]['base_sha'])


if __name__ == '__main__':
    unittest.main()
