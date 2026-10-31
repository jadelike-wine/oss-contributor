"""Publication authorization checks; no test launches an external process."""
from types import SimpleNamespace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contribute import ContributionPaused, Pipeline, enforce_external_write_policy
from telemetry import ACTIVE
from verify_ci import refresh


class ExternalWritePolicyTests(unittest.TestCase):
    def check(self, args, allow=None):
        context = None if allow is None else SimpleNamespace(p=SimpleNamespace(allow_external_writes=allow))
        token = ACTIVE.set(context)
        try:
            return enforce_external_write_policy(args)
        finally:
            ACTIVE.reset(token)

    def test_no_context_and_false_deny_all_publication_commands(self):
        commands = [
            ['git', 'push', 'https://github.com/owner/repo', 'HEAD'],
            ['git', '-c', 'http.sslBackend=openssl', 'push', 'origin', 'HEAD'],
            ['gh', 'issue', 'create', '-R', 'owner/repo'],
            ['gh', 'pr', 'create', '-R', 'owner/repo'],
            ['gh', 'pr', 'edit', '1', '-R', 'owner/repo'],
            ['gh', 'repo', 'fork', 'owner/repo', '--clone=false'],
            ['gh', 'workflow', 'run', 'verify.yml'],
            ['gh', 'api', 'repos/owner/repo/issues', '-X', 'POST'],
        ]
        for allow in (None, False):
            for args in commands:
                with self.subTest(allow=allow, args=args), self.assertRaises(ContributionPaused):
                    self.check(args, allow)

    def test_read_commands_remain_available_without_authorization(self):
        commands = [
            ['git', 'diff', 'HEAD'], ['git', 'ls-remote', 'https://github.com/owner/repo'],
            ['gh', 'api', 'repos/owner/repo'],
            ['gh', 'api', 'repos/owner/repo', '-X', 'GET'],
            ['gh', 'api', 'repos/owner/repo', '-XGET'],
            ['gh', 'api', 'repos/owner/repo', '--method=GET'],
            ['gh', 'api', 'repos/owner/repo', '--method', 'HEAD'],
            ['gh', 'pr', 'view', '1'], ['gh', 'pr', 'list'],
            ['gh', 'issue', 'view', '1'], ['gh', 'run', 'view', '1'],
            ['gh', 'repo', 'view', 'owner/repo'], ['gh', 'search', 'issues', 'text'],
        ]
        for allow in (None, False):
            for args in commands:
                with self.subTest(allow=allow, args=args):
                    self.check(args, allow)

    def test_api_mutation_method_forms_and_conflicting_methods_fail_closed(self):
        flags = [
            ['-X', 'POST'], ['-XPOST'], ['--method', 'POST'], ['--method=POST'],
            ['-X', 'PATCH'], ['-XDELETE'], ['--method=PUT'],
            ['-X', 'GET', '--method=POST'], ['-XGET', '-XPOST'],
        ]
        for flag in flags:
            with self.subTest(flag=flag), self.assertRaises(ContributionPaused):
                self.check(['gh', 'api', 'repos/owner/repo/issues', *flag])

    def test_api_fields_and_input_default_to_post_in_all_supported_forms(self):
        flags = [
            ['-f', 'title=hello'], ['-F', 'title=hello'],
            ['--field', 'title=hello'], ['--raw-field', 'title=hello'],
            ['--field=title=hello'], ['--raw-field=title=hello'],
            ['--input', 'payload.json'], ['--input=payload.json'],
            ['-ftitle=hello'], ['-Ftitle=hello'],
        ]
        for flag in flags:
            with self.subTest(flag=flag), self.assertRaises(ContributionPaused):
                self.check(['gh', 'api', 'repos/owner/repo/issues', *flag])

    def test_explicit_get_fields_are_read_only(self):
        self.check(['gh', 'api', 'search/issues', '-X', 'GET', '-f', 'q=repo:owner/repo bug'])
        self.check(['gh', 'api', 'search/issues', '--method=GET', '--field=q=repo:owner/repo'])

    def test_graphql_requires_one_inline_read_query_and_rejects_mutations(self):
        self.check(['gh', 'api', 'graphql', '-f', 'query=query { viewer { login } }'])
        self.check(['gh', 'api', 'graphql', '-f', 'query={ viewer { login } }'])
        rejected = [
            ['-f', 'query=mutation { deleteIssue(input:{id:"x"}) { clientMutationId } }'],
            ['-f', 'query=query Safe { viewer { login } } mutation Bad { deleteIssue(input:{id:"x"}) { clientMutationId } }'],
            ['-f', 'query=@query.graphql'], ['--input', 'query.json'], ['--input=query.json'],
            ['-f', 'query=query { viewer { login } }', '-f', 'query=mutation { x }'],
        ]
        for flags in rejected:
            with self.subTest(flags=flags), self.assertRaises(ContributionPaused):
                self.check(['gh', 'api', 'graphql', *flags])

    def test_only_explicit_boolean_true_authorizes_a_write(self):
        args = ['gh', 'issue', 'create', '-R', 'owner/repo']
        self.check(args, True)
        for value in ('false', 'true', 1, [], {}):
            with self.subTest(value=value), self.assertRaises(ContributionPaused):
                self.check(args, value)

    def test_refresh_can_reuse_valid_receipt_without_write_authorization(self):
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.allow_external_writes = False
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'receipt.json'
            receipt.write_text(json.dumps({'auto_refresh': True}), encoding='utf-8')
            with patch('verify_ci.verify', return_value='verified-url') as verify, \
                 patch('verify_ci.gh') as gh, patch('verify_ci.command') as command, patch('verify_ci.save') as save:
                self.assertEqual('verified-url', refresh(pipeline, 'task', receipt))
                verify.assert_called_once()
                gh.assert_not_called()
                command.assert_not_called()
                save.assert_not_called()

    def test_refresh_direct_entry_stops_before_new_remote_or_git_effects(self):
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.allow_external_writes = False
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'receipt.json'
            original = json.dumps({'auto_refresh': True})
            receipt.write_text(original, encoding='utf-8')
            with patch('verify_ci.verify', side_effect=ValueError('stale receipt')), \
                 patch('verify_ci.gh') as gh, patch('verify_ci.command') as command, patch('verify_ci.save') as save:
                with self.assertRaises(ContributionPaused):
                    refresh(pipeline, 'task', receipt)
                gh.assert_not_called()
                command.assert_not_called()
                save.assert_not_called()
                self.assertEqual(original, receipt.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
