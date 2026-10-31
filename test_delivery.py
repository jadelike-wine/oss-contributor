import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from delivery import content_fingerprint, content_tree, ensure_commit, ensure_push, node_command, windows_hook_environment, reconcile_pending, reconciliation_due


def command(args, cwd=None, env=None, **kwargs):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, encoding='utf-8', **kwargs)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout


def legacy_fingerprint(work):
    """Frozen baseline from ab5ed20, for explicit before/after evidence."""
    digest = hashlib.sha256()
    digest.update(command(['git', 'rev-parse', 'HEAD'], cwd=work).encode())
    digest.update(command(['git', 'diff', 'HEAD', '--binary'], cwd=work).encode())
    for name in sorted(filter(None, command(['git', 'ls-files', '--others', '--exclude-standard', '-z'], cwd=work).split('\0'))):
        digest.update(name.encode())
        digest.update((Path(work) / name).read_bytes())
    return digest.hexdigest()


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='oss-delivery-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Delivery test')
        self.git('config', 'user.email', 'delivery@example.invalid')
        self.git('config', 'core.autocrlf', 'false')
        (self.work / 'value.txt').write_text('old\n', encoding='utf-8')
        (self.work / '.gitignore').write_text('ignored\n', encoding='utf-8')
        self.git('add', '.')
        self.git('commit', '-qm', 'baseline')
        self.base = self.git('rev-parse', 'HEAD')
        self.saved = []

    def git(self, *args):
        return command(['git', *args], cwd=self.work).strip()

    def patch(self):
        (self.work / 'value.txt').write_text('fixed\n', encoding='utf-8')
        (self.work / 'new test.txt').write_text('regression\n', encoding='utf-8')
        return {'approved_fingerprint': content_fingerprint(self.work, command)}

    def persist(self, data):
        self.saved.append(json.loads(json.dumps(data)))

    def test_identity_survives_staging_commit_restart_and_preserves_real_index(self):
        data = self.patch()
        original_index = (self.work / '.git/index').read_bytes()
        old_before = legacy_fingerprint(self.work)
        self.assertEqual(data['approved_fingerprint'], content_fingerprint(self.work, command))
        self.assertEqual(original_index, (self.work / '.git/index').read_bytes())
        self.git('add', '--all')
        self.assertNotEqual(old_before, legacy_fingerprint(self.work), 'Old implementation must reproduce staging defect')
        self.assertEqual(data['approved_fingerprint'], content_fingerprint(self.work, command))
        commit = ensure_commit(self.work, data, 'fix', command, self.persist)
        restored = json.loads(json.dumps(self.saved[-1]))
        self.assertEqual(data['approved_fingerprint'], content_fingerprint(self.work, command))
        self.assertEqual(commit, ensure_commit(self.work, restored, 'fix', command, self.persist))
        self.assertEqual('2', self.git('rev-list', '--count', 'HEAD'))

    def test_content_deletion_and_rename_change_identity_but_ignored_files_do_not(self):
        original = self.patch()['approved_fingerprint']
        (self.work / 'ignored').write_text('cache')
        self.assertEqual(original, content_fingerprint(self.work, command))
        (self.work / 'new test.txt').rename(self.work / 'renamed test.txt')
        self.assertNotEqual(original, content_fingerprint(self.work, command))
        (self.work / 'renamed test.txt').rename(self.work / 'new test.txt')
        self.assertEqual(original, content_fingerprint(self.work, command))
        (self.work / 'value.txt').unlink()
        self.assertNotEqual(original, content_fingerprint(self.work, command))

    def test_failed_commit_recovers_without_reapproval_of_unchanged_content(self):
        data = self.patch()
        def fail(args, **kwargs):
            if args[:2] == ['git', 'commit']:
                raise RuntimeError('temporary hook tool failure')
            return command(args, **kwargs)
        with self.assertRaisesRegex(RuntimeError, 'temporary hook'):
            ensure_commit(self.work, data, 'fix', fail, self.persist)
        self.assertIn('expected_tree', self.saved[-1])
        self.assertEqual(self.base, self.git('rev-parse', 'HEAD'))
        self.assertEqual(data['approved_fingerprint'], content_fingerprint(self.work, command))
        ensure_commit(self.work, data, 'fix', command, self.persist)
        self.assertEqual('2', self.git('rev-list', '--count', 'HEAD'))

    def test_changed_content_after_failed_commit_is_rejected(self):
        data = self.patch()
        data['expected_tree'] = content_tree(self.work, command)
        (self.work / 'value.txt').write_text('unreviewed\n')
        with self.assertRaisesRegex(RuntimeError, 'changed after approval'):
            ensure_commit(self.work, data, 'fix', command, self.persist)
        self.assertEqual(self.base, self.git('rev-parse', 'HEAD'))

    def test_lost_commit_response_adopts_same_commit(self):
        data = self.patch()
        def lost(args, **kwargs):
            result = command(args, **kwargs)
            if args[:2] == ['git', 'commit']:
                raise RuntimeError('response lost')
            return result
        with self.assertRaisesRegex(RuntimeError, 'response lost'):
            ensure_commit(self.work, data, 'fix', lost, self.persist)
        head = self.git('rev-parse', 'HEAD')
        self.assertEqual(head, ensure_commit(self.work, data, 'fix', command, self.persist))
        self.assertEqual('2', self.git('rev-list', '--count', 'HEAD'))

    def test_hook_changes_cannot_be_published_with_old_approval(self):
        data = self.patch()
        def mutate(args, **kwargs):
            if args[:2] == ['git', 'commit']:
                (self.work / 'value.txt').write_text('hook change\n')
                self.git('add', '--all')
            return command(args, **kwargs)
        with self.assertRaisesRegex(RuntimeError, 'hook changed'):
            ensure_commit(self.work, data, 'fix', mutate, self.persist)
        self.assertNotIn('publish_commit', data)

    def test_push_failure_lost_response_and_restart_use_one_remote_commit(self):
        data = self.patch()
        ensure_commit(self.work, data, 'fix', command, self.persist)
        remote = self.root / 'remote.git'
        command(['git', 'init', '--bare', '-q', str(remote)])
        pushes = []
        def failed(args, **kwargs):
            if args[:2] == ['git', 'push']:
                pushes.append(args)
                raise RuntimeError('connection failed before push')
            return command(args, **kwargs)
        with self.assertRaisesRegex(RuntimeError, 'connection failed'):
            ensure_push(self.work, str(remote), 'patch', data, failed, self.persist)
        self.assertIn('push_intent', self.saved[-1])
        self.assertNotIn('push_verified', data)
        def lost(args, **kwargs):
            result = command(args, **kwargs)
            if args[:2] == ['git', 'push']:
                pushes.append(args)
                raise subprocess.TimeoutExpired(args, 1)
            return result
        ensure_push(self.work, str(remote), 'patch', data, lost, self.persist)
        self.assertEqual(2, len(pushes))
        ensure_push(self.work, str(remote), 'patch', json.loads(json.dumps(data)), lost, self.persist)
        self.assertEqual(2, len(pushes), 'Restart must reconcile remote, not push again')
        self.assertEqual(data['publish_commit'], command(['git', '--git-dir', str(remote), 'rev-parse', 'refs/heads/patch']).strip())

    def test_legacy_receipt_cannot_silently_approve_new_identity(self):
        self.patch()
        data = {'approved_fingerprint': legacy_fingerprint(self.work)}
        with self.assertRaisesRegex(RuntimeError, 'verification and review required'):
            ensure_commit(self.work, data, 'fix', command, self.persist)

    def test_pending_publication_uses_bounded_reads_then_preserves_manual_hold(self):
        data = {'pr_create_pending': True, 'branch': 'saved-branch'}
        now = dt.datetime(2026, 10, 31, tzinfo=dt.timezone.utc)
        for count in range(1, 4):
            self.assertTrue(reconciliation_due(data, now=now))
            result = reconcile_pending(data, [], observed_at=now)
            data['pr_reconciliation'] = result
            self.assertEqual(count, result['checks'])
            self.assertFalse(result['may_create'])
            self.assertTrue(data['pr_create_pending'])
            self.assertFalse(reconciliation_due(data, now=now))
            now += dt.timedelta(seconds=901)
        self.assertEqual('manual_verification', result['state'])
        self.assertFalse(reconciliation_due(data, now=now + dt.timedelta(days=30)))
        held = reconcile_pending(data, [], observed_at=now)
        self.assertEqual(3, held['checks'])
        self.assertEqual('manual_verification', held['state'])

    def test_pending_publication_recovery_and_ambiguity_never_allow_recreation(self):
        for kind in ('pr', 'issue'):
            data = {kind + '_create_pending': True}
            publication = {'number': 3, 'html_url': 'https://example.invalid/3'}
            result = reconcile_pending(data, [publication], kind=kind)
            self.assertEqual(publication, result['publication'])
            self.assertEqual('recovered', result['state'])
            self.assertFalse(result['may_create'])
            self.assertNotIn(kind + '_reconciliation', data, 'Helper must not mutate durable input')
            ambiguous = reconcile_pending(data, [publication, {'number': 4}], kind=kind)
            self.assertEqual('manual_verification', ambiguous['state'])
            self.assertFalse(ambiguous['may_create'])

    def test_corepack_pnpm_is_resolved_without_a_standalone_pnpm_package(self):
        runtime = self.root / 'Node With Spaces'
        runtime.mkdir()
        script = runtime / 'node_modules/corepack/dist/pnpm.js'
        script.parent.mkdir(parents=True)
        script.write_text('console.log("ok")')
        def which(name):
            return str(runtime / ('node.exe' if name == 'node' else 'pnpm.cmd'))
        self.assertEqual([str(runtime / 'node.exe'), str(script)], node_command('pnpm', which))

    @unittest.skipUnless(os.name == 'nt', 'Git for Windows hook path regression')
    def test_windows_hook_executes_real_node_entry_with_spaces_and_keeps_hook(self):
        runtime = self.root / 'Node With Spaces'
        runtime.mkdir()
        script = runtime / 'node_modules/corepack/dist/pnpm.js'
        script.parent.mkdir(parents=True)
        script.write_text('require("fs").writeFileSync("hook-ran", process.argv.slice(2).join(" "))', encoding='utf-8')
        def which(name):
            if name == 'node':
                return shutil.which('node')
            return str(runtime / 'pnpm.cmd') if name == 'pnpm' else None
        env = windows_hook_environment(self.root / 'hook tools', which=which)
        hook = self.work / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\npnpm test --with-flag\n', encoding='utf-8', newline='\n')
        self.patch()
        self.git('add', '--all')
        command(['git', 'commit', '-qm', 'real hook check'], cwd=self.work, env=env)
        self.assertEqual('test --with-flag', (self.work / 'hook-ran').read_text())
        self.assertEqual('2', self.git('rev-list', '--count', 'HEAD'))


if __name__ == '__main__':
    unittest.main()
