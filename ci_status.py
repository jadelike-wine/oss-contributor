"""Read-only CI observation, including workflows without check runs yet."""
import subprocess
from urllib.parse import quote


def observe_ci(repo, pr, gh):
    result = {'state': 'unknown', 'failed_checks': [], 'waiting_checks': [],
              'unknown_checks': [], 'workflow_runs': []}
    for check in pr.get('statusCheckRollup') or []:
        outcome = (check.get('conclusion') or check.get('state') or check.get('status') or '').lower()
        if outcome in ('failure', 'error', 'timed_out', 'startup_failure'):
            result['failed_checks'].append(dict(check))
        elif outcome == 'action_required':
            result['waiting_checks'].append({**check, 'reason': 'approval_required'})
        elif outcome in ('pending', 'queued', 'in_progress', 'waiting', 'requested', 'expected'):
            result['waiting_checks'].append({**check, 'reason': 'in_progress'})
        elif outcome not in ('success', 'neutral', 'skipped'):
            result['unknown_checks'].append(dict(check))
    sha = pr.get('headRefOid')
    try:
        if not isinstance(sha, str) or not sha:
            raise ValueError('Missing PR head SHA; workflow status is unverified')
        pages = gh('api', '--paginate', '--slurp',
                   f'repos/{repo}/actions/runs?head_sha={quote(sha, safe="")}&per_page=100')
        if not isinstance(pages, list):
            raise ValueError('Invalid workflow pages')
        latest = {}
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get('workflow_runs'), list):
                raise ValueError('Invalid workflow run list')
            for run in page['workflow_runs']:
                if (not isinstance(run, dict) or type(run.get('id')) is not int
                        or not isinstance(run.get('status'), str) or not run.get('head_sha')):
                    raise ValueError('Invalid workflow run')
                if run['head_sha'] != sha:
                    continue
                # Retain separate triggers, but supersede older runs/attempts of
                # the same workflow so an old failure cannot mask a rerun.
                key = (run.get('workflow_id') or run.get('path') or run.get('name') or run['id'], run.get('event'))
                order = (run['id'], run.get('run_attempt') or 1)
                if key not in latest or order > latest[key][0]:
                    latest[key] = (order, run)
        result['workflow_runs'] = [item[1] for item in latest.values()]
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired, KeyError, TypeError) as error:
        result['error'] = str(error)
    for run in result['workflow_runs']:
        item = {'name': run.get('name'), 'detailsUrl': run.get('html_url'),
                'run_id': run['id'], 'status': run['status'], 'conclusion': run.get('conclusion')}
        outcome = run.get('conclusion')
        if outcome == 'action_required':
            result['waiting_checks'].append({**item, 'reason': 'approval_required'})
        elif run['status'] != 'completed':
            result['waiting_checks'].append({**item, 'reason': 'in_progress'})
        elif outcome in ('failure', 'timed_out', 'startup_failure'):
            result['failed_checks'].append(item)
        elif outcome not in ('success', 'neutral', 'skipped'):
            result['unknown_checks'].append(item)
    if result['failed_checks']:
        result['state'] = 'failed'
    elif result.get('error') or result['unknown_checks']:
        result['state'] = 'unknown'
    elif result['waiting_checks']:
        result['state'] = ('approval_required' if any(x['reason'] == 'approval_required'
                            for x in result['waiting_checks']) else 'pending')
    elif result['workflow_runs'] or pr.get('statusCheckRollup'):
        # This describes observed checks, not an unqueried branch-protection gate.
        result['state'] = 'observed_checks_passed'
    return result
