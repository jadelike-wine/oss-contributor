import base64
import hashlib
import json
import unittest
from unittest.mock import Mock, patch

from verify_ci import verify


class RemoteVerificationTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = Mock()
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
