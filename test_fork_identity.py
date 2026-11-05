"""Renamed GitHub forks: API-verified discovery and exact publication recovery."""
import subprocess
import unittest
from unittest.mock import Mock, patch

from contribute import ContributionPaused
from delivery import ForkIdentityError, resolve_publication_fork, verified_fork
import test_delivery_integration as execution_fixture


def repository(name='bot/renamed-repo'):
    return {'full_name': name, 'owner': {'login': 'bot'}, 'fork': True,
            'parent': {'full_name': 'owner/repo'}, 'clone_url': 'https://github.com/' + name + '.git'}


class ForkDiscoveryTests(unittest.TestCase):
    def test_renamed_existing_fork_from_stderr_is_verified_by_exact_api_read(self):
        api = Mock(return_value=repository())
        execute = Mock(return_value=subprocess.CompletedProcess([], 0, '', '\u2713 bot/renamed-repo already exists'))
        fork = resolve_publication_fork('owner/repo', 'bot', api, execute)
        self.assertEqual('bot/renamed-repo', fork['full_name'])
        api.assert_called_once_with('api', 'repos/bot/renamed-repo')
        execute.assert_called_once_with(['gh', 'repo', 'fork', 'owner/repo', '--clone=false'])

    def test_creation_url_and_duplicate_cli_identity_resolve_one_verified_fork(self):
        api = Mock(return_value=repository())
        execute = Mock(return_value=subprocess.CompletedProcess([], 0,
            'https://github.com/bot/renamed-repo\n', '\x1b[32mCreated fork bot/renamed-repo\x1b[0m'))
        self.assertEqual(repository(), resolve_publication_fork('owner/repo', 'bot', api, execute))
        api.assert_called_once_with('api', 'repos/bot/renamed-repo')

    def test_empty_cli_output_checks_conventional_candidate_instead_of_trusting_it(self):
        api = Mock(return_value=repository('bot/repo'))
        execute = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        self.assertEqual('bot/repo', resolve_publication_fork('owner/repo', 'bot', api, execute)['full_name'])
        api.assert_called_once_with('api', 'repos/bot/repo')

    def test_saved_fork_is_revalidated_without_another_fork_command(self):
        api, execute = Mock(return_value=repository()), Mock(side_effect=AssertionError('No repeat fork command'))
        self.assertEqual(repository(), resolve_publication_fork('owner/repo', 'bot', api, execute,
                                                               saved='bot/renamed-repo'))
        api.assert_called_once_with('api', 'repos/bot/renamed-repo')
        execute.assert_not_called()

    def test_wrong_owner_parent_identity_nonfork_and_push_url_are_rejected(self):
        for field in ('owner', 'parent', 'full_name', 'fork', 'clone_url'):
            candidate = repository()
            candidate[field] = {'login': 'someone'} if field == 'owner' else (
                {'full_name': 'other/repo'} if field == 'parent' else False if field == 'fork' else 'unrelated')
            with self.subTest(field=field), self.assertRaises(ForkIdentityError):
                verified_fork('owner/repo', 'bot', 'bot/renamed-repo', Mock(return_value=candidate))
        for name in ('someone/repo', 'bot/../repo', 'https://github.com/bot/repo', 'bot/..'):
            api = Mock()
            with self.subTest(name=name), self.assertRaises(ForkIdentityError):
                verified_fork('owner/repo', 'bot', name, api)
            api.assert_not_called()

    def test_ambiguous_fork_response_does_not_pick_one_or_query_default_name(self):
        api = Mock()
        execute = Mock(return_value=subprocess.CompletedProcess([], 0, 'bot/first bot/second', ''))
        with self.assertRaisesRegex(ForkIdentityError, 'multiple'):
            resolve_publication_fork('owner/repo', 'bot', api, execute)
        api.assert_not_called()

    def test_failed_creation_or_api_read_does_not_become_verified_identity(self):
        api = Mock()
        execute = Mock(return_value=subprocess.CompletedProcess([], 1, '', 'connection reset'))
        with self.assertRaisesRegex(RuntimeError, 'connection reset'):
            resolve_publication_fork('owner/repo', 'bot', api, execute)
        api.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'network unavailable'):
            verified_fork('owner/repo', 'bot', 'bot/renamed-repo', Mock(side_effect=RuntimeError('network unavailable')))


class RenamedForkDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fx = execution_fixture.DeliveryIntegrationTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.addCleanup(self.fx.tearDown)
        self.fx.fork_name = 'bot/renamed-repo'

    def pending(self):
        fx = self.fx
        data = fx.prepared()
        fx.lose_pr_response = True
        with fx.mocks(), self.assertRaisesRegex(RuntimeError, 'Network lost'):
            fx.engine.publish_pr(fx.key, data)
        return fx.engine.load(fx.key)

    def test_renamed_fork_publishes_and_saves_actual_head_before_lost_response(self):
        fx = self.fx
        data = self.pending()
        self.assertEqual('bot/renamed-repo', data['publication_fork']['full_name'])
        self.assertEqual('bot/renamed-repo', data['pr_publication_identity']['head_repo'])
        remote = subprocess.check_output(['git', 'ls-remote', str(fx.fork), 'refs/heads/' + data['branch']], text=True)
        self.assertEqual(data['publish_commit'], remote.split()[0])
        with fx.mocks(), patch('autonomous.command', side_effect=AssertionError('Read-only recovery')), \
                patch('autonomous.execute', side_effect=AssertionError('No fork or model command')):
            fx.engine.publish_pr(fx.key, data)
        self.assertEqual('PR_OPEN', data['stage'])
        self.assertFalse(data['pr_create_pending'])
        self.assertEqual(1, len(fx.created_prs))

    def test_legacy_pending_renamed_head_is_verified_without_basename_guess_or_writes(self):
        fx = self.fx
        data = self.pending()
        data.pop('pr_publication_identity')
        data.pop('publication_fork')
        calls = []
        def api(*args):
            calls.append(args)
            return fx.fake_gh(*args)
        with fx.mocks(), patch('autonomous.gh', side_effect=api), \
                patch('autonomous.command', side_effect=AssertionError('Read-only recovery')), \
                patch('autonomous.execute', side_effect=AssertionError('No fork or model command')):
            fx.engine.publish_pr(fx.key, data)
        self.assertIn(('api', 'repos/bot/renamed-repo'), calls)
        self.assertNotIn(('api', 'repos/bot/repo'), calls)
        self.assertEqual('bot/renamed-repo', data['pr_publication_identity']['head_repo'])
        self.assertEqual('PR_OPEN', data['stage'])
        self.assertEqual(1, len(fx.created_prs))

    def test_legacy_pending_wrong_fork_parent_remains_unpublished_locally(self):
        fx = self.fx
        data = self.pending()
        data.pop('pr_publication_identity')
        data.pop('publication_fork')
        def api(*args):
            if args == ('api', 'repos/bot/renamed-repo'):
                wrong = repository()
                wrong['parent']['full_name'] = 'other/repo'
                return wrong
            return fx.fake_gh(*args)
        with fx.mocks(), patch('autonomous.gh', side_effect=api), \
                patch('autonomous.execute', side_effect=AssertionError('No writes')), \
                self.assertRaisesRegex(ContributionPaused, 'fork identity'):
            fx.engine.publish_pr(fx.key, data)
        self.assertTrue(data['pr_create_pending'])
        self.assertEqual('manual_verification', data['pr_reconciliation']['state'])
        self.assertNotIn('pr_number', data)


if __name__ == '__main__':
    unittest.main()
