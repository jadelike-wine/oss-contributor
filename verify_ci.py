"""Verify a completed GitHub Actions run against this exact upstream patch."""
import argparse
import base64
import hashlib
import json

from contribute import Pipeline, command, gh, repo_name


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
