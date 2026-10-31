"""Content identity and restart-safe local delivery primitives.

Callers retain policy, freshness, review and external-write authorization gates.
These helpers never infer that a successful test grants publication permission.
"""
import datetime as dt
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile


def isolated_worktree_args(destination, revision):
    """Create only new worktrees with LF defaults; repository attributes win.

Per-command settings do not alter shared/global configuration or rewrite any
existing workspace. This keeps host autocrlf settings from manufacturing test
failures in upstream LF inputs such as shebangs and Markdown fixtures.
"""
    return ['git', '-c', 'core.autocrlf=false', '-c', 'core.eol=lf',
            'worktree', 'add', '--detach', str(destination), revision]


def content_tree(work, command):
    """Return the tree Git would commit, without touching the real index.

Git's clean filters and executable/symlink modes are part of commit content.
HEAD metadata and whether a new file has been staged are not. A private index
also means a failed check cannot destroy the user's partial staging.
"""
    if command(['git', 'ls-files', '-u'], cwd=work).strip():
        raise RuntimeError('Cannot identify a patch with unresolved index conflicts')
    with tempfile.TemporaryDirectory(prefix='oss-content-index-') as directory:
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(directory) / 'index')}
        command(['git', 'read-tree', 'HEAD'], cwd=work, env=env)
        command(['git', 'add', '--all', '--', '.'], cwd=work, env=env)
        return command(['git', 'write-tree'], cwd=work, env=env).strip()


def content_fingerprint(work, command):
    # A version prefix intentionally makes legacy receipts fail closed. They
    # must be revalidated; there is no safe automatic conversion from a hash.
    return 'tree-v2:' + content_tree(work, command)


def ensure_commit(work, data, title, command, persist):
    """Commit an approved tree or adopt its existing commit after interruption.

``persist`` must durably save ``data`` before returning. Hooks stay enabled.
Any hook-induced content change invalidates approval, even if commit succeeded.
"""
    tree = content_tree(work, command)
    if data.get('approved_fingerprint') != 'tree-v2:' + tree:
        raise RuntimeError('Patch changed after approval; verification and review required')
    previous_tree = data.get('expected_tree')
    if previous_tree and previous_tree != tree:
        raise RuntimeError('Publication intent refers to different content')
    head_tree = command(['git', 'rev-parse', 'HEAD^{tree}'], cwd=work).strip()
    if head_tree == tree:
        if not previous_tree:
            raise RuntimeError('No approved patch commit or recoverable publication intent')
    else:
        data['expected_tree'] = tree
        persist(data)
        command(['git', 'add', '--all'], cwd=work)
        if command(['git', 'write-tree'], cwd=work).strip() != tree:
            raise RuntimeError('Patch changed while staging; verification and review required')
        command(['git', 'commit', '-m', title], cwd=work)
    if command(['git', 'rev-parse', 'HEAD^{tree}'], cwd=work).strip() != tree or content_tree(work, command) != tree:
        raise RuntimeError('Commit hook changed approved content; verification and review required')
    data['publish_commit'] = command(['git', 'rev-parse', 'HEAD'], cwd=work).strip()
    persist(data)
    return data['publish_commit']


def ensure_push(work, remote, branch, data, command, persist, **kwargs):
    """Reconcile exact remote head before/after a non-forced push.

The intent is durable before the write. A lost push response is accepted only
when a subsequent read proves the exact intended commit reached that branch.
"""
    commit = data['publish_commit']
    ref = 'refs/heads/' + branch
    def observed():
        lines = command(['git', 'ls-remote', '--heads', remote, ref], cwd=work,
                        **{k: v for k, v in kwargs.items() if k in ('timeout', 'env')}).splitlines()
        matches = [line.split()[0] for line in lines if len(line.split()) == 2 and line.split()[1] == ref]
        if len(matches) > 1:
            raise RuntimeError('Ambiguous remote branch identity')
        return matches[0] if matches else None
    if observed() != commit:
        data['push_intent'] = {'remote': remote, 'ref': ref, 'commit': commit}
        persist(data)
        try:
            command(['git', 'push', remote, commit + ':' + ref], cwd=work, **kwargs)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            try:
                reached = observed() == commit
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                reached = False
            if not reached:
                raise error
        if observed() != commit:
            raise RuntimeError('Push response did not establish the intended remote head')
    data['push_verified'] = {'ref': ref, 'commit': commit}
    persist(data)
    return commit


def reconciliation_due(data, kind='pr', now=None):
    """Whether another automatic *read* may reconcile a pending publication."""
    receipt = data.get(kind + '_reconciliation') or {}
    if receipt.get('state') == 'manual_verification':
        return False
    retry = receipt.get('retry_after')
    if not retry:
        return True
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        return now >= dt.datetime.fromisoformat(retry)
    except (ValueError, TypeError):
        # Malformed durable intent must not enable a fresh external write.
        return False


def reconcile_pending(data, matches, kind='pr', max_checks=3, observed_at=None,
                      cooldown_seconds=900):
    """Describe a completed read without ever authorizing another creation.

The caller supplies a complete, successful query scoped to the original
publication identity, persists the returned receipt under ``<kind>_reconciliation``,
and keeps ``<kind>_create_pending`` until a unique publication is recovered.
Network failures must propagate rather than become an empty match list.
"""
    if kind not in ('pr', 'issue') or not data.get(kind + '_create_pending'):
        raise ValueError('Reconciliation requires an existing publication intent')
    if not isinstance(matches, list) or max_checks < 1:
        raise ValueError('Reconciliation requires a complete result list and a positive bound')
    observed_at = observed_at or dt.datetime.now(dt.timezone.utc)
    previous = data.get(kind + '_reconciliation') or {}
    checks = int(previous.get('checks', 0))
    result = {'checks': checks, 'checked_at': observed_at.isoformat(),
              'intent_preserved': True, 'may_create': False}
    if len(matches) == 1:
        return {**result, 'state': 'recovered', 'publication': matches[0],
                'reason': 'Existing publication found by its saved identity'}
    if len(matches) > 1:
        return {**result, 'state': 'manual_verification',
                'reason': 'Multiple publications match the saved intent; manual verification required'}
    if previous.get('state') == 'manual_verification':
        return {**result, 'state': 'manual_verification', 'reason': previous['reason']}
    checks += 1
    result['checks'] = checks
    if checks >= max_checks:
        return {**result, 'state': 'manual_verification',
                'reason': 'Publication response is unresolved after bounded reads; manual verification required before any retry'}
    return {**result, 'state': 'retry_wait',
            'retry_after': (observed_at + dt.timedelta(seconds=cooldown_seconds)).isoformat(),
            'reason': 'Publication response unresolved; retain intent and retry a read after cooldown'}


NODE_ENTRIES = {
    'npm': ('npm/bin/npm-cli.js',),
    'pnpm': ('pnpm/bin/pnpm.cjs', 'corepack/dist/pnpm.js'),
    'corepack': ('corepack/dist/corepack.js',),
}


def node_command(name, which=shutil.which):
    """Resolve Windows package-manager shims, including Corepack's pnpm shim."""
    key = Path(name).stem.lower()
    if key not in NODE_ENTRIES:
        return None
    launcher, node = which(name), which('node')
    if not launcher or not node:
        raise ValueError('Missing Node entry point for ' + key)
    for relative in NODE_ENTRIES[key]:
        script = Path(launcher).parent / 'node_modules' / relative
        if script.is_file():
            return [node, str(script)]
    raise ValueError('Missing Node entry point for ' + key)


def windows_hook_environment(directory, env=None, which=shutil.which):
    """Give Git's POSIX hooks absolute, correctly quoted Node entry points.

Corepack's standard shell shim can turn /c/... into C:\\c\\... under Git
for Windows. A small local shim avoids that conversion without disabling hooks.
"""
    result = dict(os.environ if env is None else env)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name in NODE_ENTRIES:
        if not which(name):
            continue
        argv = node_command(name, which)
        body = '#!/bin/sh\nexec ' + ' '.join(shlex.quote(str(Path(value)).replace('\\', '/')) for value in argv) + ' "$@"\n'
        # The directory belongs to the controller; never overwrite upstream hooks.
        target = directory / name
        if not target.exists() or target.read_text(encoding='utf-8') != body:
            target.write_text(body, encoding='utf-8', newline='\n')
    result['PATH'] = str(directory.resolve()) + os.pathsep + result.get('PATH', '')
    return result
