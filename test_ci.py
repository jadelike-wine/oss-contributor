import base64
import hashlib
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from verify_ci import verify, refresh


class RemoteVerificationTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = Mock()
        self.pipeline.config = {}
        self.pipeline.task.return_value = {'repo': 'owner/upstream', 'base_sha': 'base', 'workspace': 'work'}
        self.receipt = {'repository': 'owner/controller', 'run_id': 123, 'head_sha': 'commit',
                        'workflow_path': '.github/workflows/check.yml', 'manifest_path': 'ci/patch.json'}
        self.run = {'status': 'completed', 'conclusion': 'success', 'head_sha': 'commit',
                    'path': self.receipt['workflow_path'], 'event': 'workflow_dispatch', 'html_url': 'https://github.com/run'}
        self.manifest = {'repository': 'owner/upstream', 'base_sha': 'base',
                         'patch_sha256': hashlib.sha256(b'patch').hexdigest()}

    def call(self, untracked='', diff='patch'):
        content = {'content': base64.b64encode(json.dumps(self.manifest).encode()).decode()}
        with patch('verify_ci.gh', side_effect=[self.run, content]), patch('verify_ci.command', side_effect=[untracked, diff]):
            return verify(self.pipeline, 'task', self.receipt)

    def test_refresh_resumes_pending_run_and_rechecks_exact_patch(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / 'remote-verification.json'
            receipt = {**self.receipt, 'auto_refresh': True, 'workflow_path': '.github/workflows/verify-opencode.yml',
                       'manifest_path': 'ci/patches/opencode-55694.json'}
            file.write_text(json.dumps(receipt))
            pending = {**receipt, 'head_sha': 'new-commit', 'branch': 'ci-test', 'patch_sha256': hashlib.sha256(b'patch').hexdigest()}
            file.with_name('pending-remote-verification.json').write_text(json.dumps(pending))
            self.pipeline.task.return_value['repo'] = 'anomalyco/opencode'
            run = {**self.run, 'head_sha': 'new-commit', 'id': 456}
            replies = [{'owner': {'login': 'owner'}, 'private': True}, {'login': 'owner'},
                       {'workflow_runs': [run]}, {'workflow_runs': [run]}]
            with patch('verify_ci.verify', side_effect=[ValueError('old patch'), 'verified']) as check, \
                 patch('verify_ci.gh', side_effect=replies), patch('verify_ci.command', side_effect=['', 'patch']) as cmd:
                self.assertEqual('verified', refresh(self.pipeline, 'task', file))
                self.assertEqual(2, cmd.call_count)  # No push or duplicate dispatch.
                self.assertEqual(456, check.call_args.args[2]['run_id'])
            self.assertEqual('new-commit', json.loads(file.read_text())['head_sha'])

    def test_refresh_without_opt_in_cannot_replace_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / 'receipt.json'
            file.write_text(json.dumps(self.receipt))
            with patch('verify_ci.verify', side_effect=ValueError('stale patch')), patch('verify_ci.command') as cmd:
                with self.assertRaisesRegex(ValueError, 'stale patch'):
                    refresh(self.pipeline, 'task', file)
                cmd.assert_not_called()
            self.assertEqual(self.receipt, json.loads(file.read_text()))

    def test_exact_success(self):
        self.assertEqual(self.call(), self.run['html_url'])

    def test_rejects_failed_or_unrelated_run(self):
        for field, value in [('conclusion', 'failure'), ('status', 'in_progress'), ('head_sha', 'other'),
                             ('path', 'other.yml'), ('event', 'push')]:
            with self.subTest(field=field):
                original = self.run[field]
                self.run[field] = value
                with self.assertRaises(ValueError):
                    self.call()
                self.run[field] = original

    def test_rejects_unverified_changes(self):
        for args in ({'untracked': 'new.ts'}, {'diff': 'changed patch'}):
            with self.assertRaises(ValueError):
                self.call(**args)
        self.manifest['base_sha'] = 'different base'
        with self.assertRaises(ValueError):
            self.call()


if __name__ == '__main__':
    unittest.main()
