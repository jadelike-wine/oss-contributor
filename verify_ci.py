"""Verify a completed GitHub Actions run against this exact upstream patch."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from contribute import Pipeline, command, gh, repo_name, save


def refresh(pipeline, task_id, receipt_file):
    """Re-run the configured OpenCode Linux check on an immutable CI branch."""
    receipt_file = Path(receipt_file)
    receipt = json.loads(receipt_file.read_text(encoding='utf-8'))
    try:
        return verify(pipeline, task_id, receipt)
    except ValueError:
        if not receipt.get('auto_refresh'):
            raise
    task = pipeline.task(task_id)
    if (task['repo'] != 'anomalyco/opencode' or receipt['workflow_path'] != '.github/workflows/verify-opencode.yml'
            or receipt['manifest_path'] != 'ci/patches/opencode-55694.json'):
        raise ValueError('No automatic CI recipe configured for this contribution')
    repo = repo_name(receipt['repository'])
    remote = gh('api', f'repos/{repo}')
    if remote['owner']['login'].lower() != gh('api', 'user')['login'].lower() or not remote['private']:
        raise ValueError('CI refresh requires the authenticated user private controller repository')
    if command(['git', 'ls-files', '--others', '--exclude-standard'], cwd=task['workspace']).strip():
        raise ValueError('Untracked files are not covered by CI refresh')
    patch = command(['git', 'diff', task['base_sha'], '--binary'], cwd=task['workspace'])
    sha = hashlib.sha256(patch.encode()).hexdigest()
    pending_file = receipt_file.with_name('pending-remote-verification.json')
    pending = json.loads(pending_file.read_text(encoding='utf-8')) if pending_file.exists() else {}
    if pending.get('patch_sha256') != sha:
        # A separate index preserves the operator's working tree and staging area.
        with tempfile.TemporaryDirectory(dir=pipeline.root / 'state') as temp:
            env = {**os.environ, 'GIT_INDEX_FILE': str(Path(temp) / 'index')}
            head = command(['git', 'rev-parse', 'HEAD'], cwd=pipeline.root).strip()
            command(['git', 'read-tree', head], cwd=pipeline.root, env=env)
            manifest = {'repository': task['repo'], 'base_sha': task['base_sha'], 'patch_sha256': sha}
            for name, content in ((receipt['manifest_path'], json.dumps(manifest, indent=2) + '\n'),
                                  ('ci/patches/opencode-55694.patch', patch)):
                file = Path(temp) / Path(name).name
                file.write_bytes(content.encode())
                blob = command(['git', 'hash-object', '-w', str(file)], cwd=pipeline.root).strip()
                command(['git', 'update-index', '--add', '--cacheinfo', f'100644,{blob},{name}'], cwd=pipeline.root, env=env)
            tree = command(['git', 'write-tree'], cwd=pipeline.root, env=env).strip()
            commit = command(['git', 'commit-tree', tree, '-p', head, '-m', 'test: verify updated OpenCode contribution'], cwd=pipeline.root).strip()
        branch = 'ci-' + commit[:24]
        command(['git', 'push', remote['clone_url'], f'{commit}:refs/heads/{branch}'], cwd=pipeline.root, timeout=120)
        pending = {**receipt, 'head_sha': commit, 'branch': branch, 'patch_sha256': sha}
        pending.pop('run_id', None)
        save(pending_file, pending)
    # Recover a dispatch whose response was lost before requesting another run.
    endpoint = f"repos/{repo}/actions/workflows/verify-opencode.yml/runs?branch={pending['branch']}&event=workflow_dispatch&per_page=100"
    runs = gh('api', endpoint)['workflow_runs']
    if not runs and not pending.get('dispatched'):
        command(['gh', 'workflow', 'run', 'verify-opencode.yml', '-R', repo, '--ref', pending['branch']])
        pending['dispatched'] = True
        save(pending_file, pending)
    from telemetry import ACTIVE
    current = ACTIVE.get()
    wait_seconds = pipeline.config.get('timeouts', {}).get('remote_ci_seconds', 1800)
    deadline = time.monotonic() + min(wait_seconds, current.remaining() if current else wait_seconds)
    last_status = None
    while time.monotonic() < deadline:
        runs = gh('api', endpoint)['workflow_runs']
        run = next((r for r in runs if r['head_sha'] == pending['head_sha']), None)
        status = (run['status'], run['conclusion']) if run else ('waiting', None)
        if status != last_status:
            print('Remote CI:', status, run['html_url'] if run else pending['branch'], flush=True)
            last_status = status
        if run and run['status'] == 'completed':
            candidate = {**receipt, 'head_sha': pending['head_sha'], 'run_id': run['id']}
            url = verify(pipeline, task_id, candidate)
            save(receipt_file, candidate)
            return url
        time.sleep(min(10, max(0, deadline - time.monotonic())))
    raise RuntimeError('Remote CI is still pending; next tick will resume the same run')


def verify(pipeline, task_id, receipt):
    task = pipeline.task(task_id)
    repo = repo_name(receipt['repository'])
    run = gh('api', f"repos/{repo}/actions/runs/{int(receipt['run_id'])}")
    if (run['status'] != 'completed' or run['conclusion'] != 'success'
            or run['head_sha'] != receipt['head_sha']
            or run['path'] != receipt['workflow_path']
            or run['event'] != 'workflow_dispatch'):
        raise ValueError('Remote workflow has not passed for the expected immutable revision')
    content = gh('api', f"repos/{repo}/contents/{receipt['manifest_path']}?ref={receipt['head_sha']}")
    manifest = json.loads(base64.b64decode(content['content']))
    if manifest['repository'] != task['repo'] or manifest['base_sha'] != task['base_sha']:
        raise ValueError('Remote verification targets a different upstream revision')
    if command(['git', 'ls-files', '--others', '--exclude-standard'], cwd=task['workspace']).strip():
        raise ValueError('Untracked files are not covered by the remote patch')
    patch = command(['git', 'diff', task['base_sha'], '--binary'], cwd=task['workspace'])
    if hashlib.sha256(patch.encode()).hexdigest() != manifest['patch_sha256']:
        raise ValueError('Local patch differs from the patch tested remotely')
    return run['html_url']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('task')
    parser.add_argument('receipt')
    args = parser.parse_args()
    with open(args.receipt, encoding='utf-8') as file:
        receipt = json.load(file)
    print('Exact patch passed remote verification: ' + verify(Pipeline(), args.task, receipt))
