"""Controller delivery tests: fake GitHub, real temporary Git and bare remote."""
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch

import test_autonomous as fixtures
from contribute import ContributionPaused


class DeliveryIntegrationTests(unittest.TestCase):
    git = fixtures.AutonomousTests.git
    model = fixtures.AutonomousTests.model
    fake_gh = fixtures.AutonomousTests.fake_gh
    fake_command = fixtures.AutonomousTests.fake_command
    tearDown = fixtures.AutonomousTests.tearDown

    def mocks(self):
        stack = fixtures.AutonomousTests.mocks(self)
        stack.enter_context(patch('contribute.inspect_repository', return_value=(
            {'full_name': 'owner/repo'}, 'fixture README', 'Small bug fixes welcome.', None, None)))
        stack.enter_context(patch('contribute.gh', side_effect=self.fake_gh))
        return stack

    def setUp(self):
        real_popen = subprocess.Popen
        def local_only(args, *positional, **kwargs):
            if isinstance(args, str) or kwargs.get('shell'):
                raise AssertionError('Test forbids shell command strings')
            tool = Path(str(args[0])).stem.lower()
            if tool == 'gh':
                raise AssertionError('Test forbids every real gh subprocess, including reads')
            if tool == 'git' and 'push' in args:
                remote = args[args.index('push') + 1]
                if not hasattr(self, 'root') or not Path(remote).resolve().is_relative_to(self.root.resolve()):
                    raise AssertionError('Test permits pushes only to its temporary local bare remote')
            return real_popen(args, *positional, **kwargs)
        guard = patch('subprocess.Popen', side_effect=local_only)
        guard.start()
        self.addCleanup(guard.stop)
        fixtures.AutonomousTests.setUp(self)
        # This fixture's GitHub writes are intercepted and pushes use a local
        # temporary bare repository. It never authorizes live external writes.
        self.p.allow_external_writes = True

    def prepared(self):
        self.model(self.key, 'plan', '')
        self.model(self.key, 'repair', '')
        data = {'stage': 'PATCH_READY', 'attempts': {}, 'plan': self.plan}
        self.engine.store(self.key, data, 'PATCH_READY')
        with self.mocks():
            self.engine.verify(self.key, data)
            self.engine.review(self.key, data)
        return data

    def test_publish_recovers_failed_commit_with_staged_new_test(self):
        data = self.prepared()
        def failed(args, **kwargs):
            if args[:2] == ['git', 'commit']:
                raise RuntimeError('Windows hook tool path failure')
            if args[:2] == ['git', 'read-tree']:
                self.assertIn('GIT_INDEX_FILE', kwargs.get('env') or {}, 'Wrapper must preserve private index')
            return self.fake_command(args, **kwargs)
        with self.mocks(), patch('autonomous.command', side_effect=failed):
            with self.assertRaisesRegex(RuntimeError, 'hook tool path'):
                self.engine.publish_pr(self.key, data)
        self.assertIn('A  test_value.py', self.git('status', '--porcelain'))
        restored = self.engine.load(self.key)
        self.assertEqual(data['approved_fingerprint'], self.p.fingerprint(self.work))
        with self.mocks():
            self.engine.publish_pr(self.key, restored)
        self.assertEqual('PR_OPEN', restored['stage'])
        self.assertEqual(1, len(self.created_prs))
        self.assertEqual('2', self.git('rev-list', '--count', 'HEAD'))

    def test_publish_recovers_lost_commit_response_without_second_commit(self):
        data = self.prepared()
        def lost(args, **kwargs):
            result = self.fake_command(args, **kwargs)
            if args[:2] == ['git', 'commit']:
                raise RuntimeError('commit response lost')
            return result
        with self.mocks(), patch('autonomous.command', side_effect=lost):
            with self.assertRaisesRegex(RuntimeError, 'commit response lost'):
                self.engine.publish_pr(self.key, data)
        head = self.git('rev-parse', 'HEAD')
        with self.mocks():
            self.engine.publish_pr(self.key, self.engine.load(self.key))
        self.assertEqual(head, self.git('rev-parse', 'HEAD'))
        self.assertEqual(1, len(self.created_prs))

    def test_publish_recovers_failed_push_then_lost_push_response(self):
        data = self.prepared()
        calls = []
        def failed(args, **kwargs):
            if args[:2] == ['git', 'push']:
                calls.append(args)
                raise RuntimeError('push unavailable')
            return self.fake_command(args, **kwargs)
        with self.mocks(), patch('autonomous.command', side_effect=failed):
            with self.assertRaisesRegex(RuntimeError, 'push unavailable'):
                self.engine.publish_pr(self.key, data)
        self.assertEqual([], self.created_prs)
        head = self.git('rev-parse', 'HEAD')
        def lost(args, **kwargs):
            result = self.fake_command(args, **kwargs)
            if args[:2] == ['git', 'push']:
                calls.append(args)
                raise subprocess.TimeoutExpired(args, 1)
            return result
        with self.mocks(), patch('autonomous.command', side_effect=lost):
            self.engine.publish_pr(self.key, self.engine.load(self.key))
        self.assertEqual(head, self.git('rev-parse', 'HEAD'))
        self.assertEqual(2, len(calls))
        self.assertEqual(1, len(self.created_prs))

    def test_publish_rejects_changed_content_after_failed_commit(self):
        data = self.prepared()
        def failed(args, **kwargs):
            if args[:2] == ['git', 'commit']:
                raise RuntimeError('temporary tool failure')
            return self.fake_command(args, **kwargs)
        with self.mocks(), patch('autonomous.command', side_effect=failed):
            with self.assertRaisesRegex(RuntimeError, 'temporary tool failure'):
                self.engine.publish_pr(self.key, data)
        (self.work / 'value.py').write_text('VALUE = 999\n', encoding='utf-8')
        with self.mocks():
            with self.assertRaisesRegex(RuntimeError, 'changed after approval'):
                self.engine.publish_pr(self.key, self.engine.load(self.key))
        self.assertEqual([], self.created_prs)
        self.assertEqual(self.base, self.git('rev-parse', 'HEAD'))

    def test_lost_pr_response_recovers_existing_pr_without_new_push(self):
        data = self.prepared()
        self.lose_pr_response = True
        with self.mocks():
            with self.assertRaisesRegex(RuntimeError, 'Network lost'):
                self.engine.publish_pr(self.key, data)
        head = self.git('rev-parse', 'HEAD')
        def no_push(args, **kwargs):
            self.assertNotEqual(['git', 'push'], args[:2], 'Remote head should already match')
            return self.fake_command(args, **kwargs)
        with self.mocks(), patch('autonomous.command', side_effect=no_push):
            self.engine.publish_pr(self.key, self.engine.load(self.key))
        self.assertEqual(head, self.git('rev-parse', 'HEAD'))
        self.assertEqual(1, len(self.created_prs))
        self.assertEqual('PR_OPEN', self.engine.load(self.key)['stage'])

    def test_subprocess_guard_blocks_every_real_github_call_and_nonlocal_push(self):
        with self.assertRaisesRegex(AssertionError, 'real gh subprocess'):
            subprocess.run(['gh', 'repo', 'fork', 'owner/repo'])
        with self.assertRaisesRegex(AssertionError, 'real gh subprocess'):
            subprocess.check_output(['gh', 'api', 'user'])
        with self.assertRaisesRegex(AssertionError, 'temporary local bare remote'):
            subprocess.run(['git', 'push', 'https://github.com/owner/repo', 'HEAD'])

    def test_changed_content_can_publish_only_after_fresh_verification_and_review(self):
        data = self.prepared()
        def failed(args, **kwargs):
            if args[:2] == ['git', 'commit']:
                raise RuntimeError('temporary tool failure')
            return self.fake_command(args, **kwargs)
        with self.mocks(), patch('autonomous.command', side_effect=failed):
            with self.assertRaisesRegex(RuntimeError, 'temporary tool failure'):
                self.engine.publish_pr(self.key, data)
        previous_tree = data['expected_tree']
        (self.work / 'value.py').write_text('VALUE = 2  # reviewed clarification\n', encoding='utf-8')
        self.engine.store(self.key, data, 'PATCH_READY')
        with self.mocks():
            self.engine.verify(self.key, data)
            self.engine.review(self.key, data)
            self.assertNotIn('expected_tree', data)
            self.assertEqual(previous_tree, data['publication_history'][-1]['previous']['expected_tree'])
            self.engine.publish_pr(self.key, data)
        self.assertEqual(1, len(self.created_prs))

    def test_unresolved_pr_intent_stops_reads_after_bounded_reconciliation(self):
        data = self.prepared()
        data.update(pr_create_pending=True, branch='saved-branch')
        self.engine.store(self.key, data)
        with self.mocks(), patch.object(self.engine, 'live_issue', return_value={'state': 'open', 'number': 1}), \
                patch.object(self.engine, 'flat', return_value=[]) as lookup:
            for count in range(1, 4):
                with self.assertRaises(ContributionPaused):
                    self.engine.publish_pr(self.key, data)
                self.assertEqual(count, lookup.call_count)
                self.assertTrue(data['pr_create_pending'])
                with self.assertRaises(ContributionPaused):
                    self.engine.publish_pr(self.key, data)
                self.assertEqual(count, lookup.call_count, 'Cooling down or manual hold must skip another query')
                data['pr_reconciliation']['retry_after'] = '2000-01-01T00:00:00+00:00'
            self.assertEqual('manual_verification', data['pr_reconciliation']['state'])
            with self.assertRaises(ContributionPaused):
                self.engine.publish_pr(self.key, data)
            self.assertEqual(3, lookup.call_count)
        self.assertEqual([], self.created_prs)
        self.assertEqual(self.base, self.git('rev-parse', 'HEAD'))

    def test_ambiguous_pending_pr_does_not_adopt_first_match(self):
        data = self.prepared()
        data.update(pr_create_pending=True, branch='saved-branch')
        with self.mocks(), patch.object(self.engine, 'live_issue', return_value={'state': 'open', 'number': 1}), \
                patch.object(self.engine, 'flat', return_value=[
            {'number': 3, 'state': 'closed', 'merged_at': None}, {'number': 4, 'state': 'open'}]):
            with self.assertRaisesRegex(ContributionPaused, 'Multiple publications'):
                self.engine.publish_pr(self.key, data)
        self.assertEqual('PUBLISH', data['stage'])
        self.assertTrue(data['pr_create_pending'])
        self.assertEqual([], self.created_prs)


if __name__ == '__main__':
    unittest.main()
