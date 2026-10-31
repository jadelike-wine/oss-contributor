"""Real Git checks for deterministic new validation worktrees on Windows."""
from pathlib import Path
import subprocess
import tempfile
import unittest

from delivery import isolated_worktree_args


class IsolatedCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='oss-checkout-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Checkout test')
        self.git('config', 'user.email', 'checkout@example.invalid')
        self.git('config', 'core.autocrlf', 'true')
        self.git('config', 'core.eol', 'crlf')
        self.script = b'#!/usr/bin/env node\nexport const value = 1;\n'
        self.markdown = b'# Diagram\n\n> This diagram is experimental.\n'
        (self.source / 'scope.mjs').write_bytes(self.script)
        (self.source / 'README.md').write_bytes(self.markdown)
        (self.source / 'native.windows.txt').write_bytes(b'explicit platform data\n')
        (self.source / '.gitattributes').write_bytes(b'*.windows.txt text eol=crlf\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'upstream LF content')
        self.sha = self.git('rev-parse', 'HEAD')

    def git(self, *args):
        result = subprocess.run(['git', *args], cwd=self.source, capture_output=True, text=True, encoding='utf-8')
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()

    def checkout(self, name, cwd=None):
        destination = self.root / name
        result = subprocess.run(isolated_worktree_args(destination, self.sha), cwd=cwd or self.source,
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(0, result.returncode, result.stderr)
        return destination

    def test_new_patch_workspace_preserves_upstream_lf_shebang(self):
        work = self.checkout('patch')
        self.assertEqual(self.script, (work / 'scope.mjs').read_bytes())

    def test_baseline_from_another_worktree_preserves_upstream_markdown_lf(self):
        patch = self.checkout('patch')
        baseline = self.checkout('baseline', cwd=patch)
        self.assertEqual(self.markdown, (baseline / 'README.md').read_bytes())

    def test_explicit_repository_crlf_attribute_remains_authoritative(self):
        work = self.checkout('patch')
        self.assertEqual(b'explicit platform data\r\n', (work / 'native.windows.txt').read_bytes())

    def test_existing_source_files_index_and_configuration_are_not_rewritten(self):
        (self.source / 'README.md').write_bytes(b'Operator draft\r\n')
        (self.source / 'operator-notes.txt').write_bytes(b'Preserve untracked work\r\n')
        config = (self.source / '.git/config').read_bytes()
        index = (self.source / '.git/index').read_bytes()
        self.checkout('patch')
        self.assertEqual(config, (self.source / '.git/config').read_bytes())
        self.assertEqual(index, (self.source / '.git/index').read_bytes())
        self.assertEqual(b'Operator draft\r\n', (self.source / 'README.md').read_bytes())
        self.assertEqual(b'Preserve untracked work\r\n', (self.source / 'operator-notes.txt').read_bytes())
        self.assertEqual('true', self.git('config', '--local', '--get', 'core.autocrlf'))
        self.assertEqual('crlf', self.git('config', '--local', '--get', 'core.eol'))


if __name__ == '__main__':
    unittest.main()
