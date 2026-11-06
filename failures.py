"""Failure domains and bounded retry state; quality attempts stay separate."""
import copy
import datetime as dt
import json
import re
import subprocess

TRANSIENT = {'network', 'rate_limit', 'certificate', 'service_unavailable', 'model_format', 'timeout'}
MODEL_FORMAT = ('invalid reviewer verdict', 'invalid local scope review', 'model must return', 'invalid model', 'ambiguous model response',
                'resolved overlap requires a revised independent scope',
                'resolved overlap revision must retain feature_key',
                'missing executable verification spec', 'missing spec field', 'recipe fields must',
                'empty test recipe', 'commands must be nonempty argv arrays', 'invalid planning decision',
                'invalid feedback decision', 'invalid research decision', 'research needs a reason',
                'invalid opportunity category/risk', 'missing opportunity field', 'one or two specific duplicate queries',
                'explicit upstream policy assessment required', 'version 2 research requires feature_matrix',
                'stable kebab-case feature_key required', 'unknown feature taxonomy category',
                'missing matrix field', 'missing bounded matrix plan', 'conflicting feature_status maps',
                'invalid feature status/reason')


def category(error):
    text = str(error).lower()
    if type(error).__name__ == 'SetupError':
        return 'setup_error'
    if type(error).__name__ == 'ContributionPaused':
        return 'policy'
    if (type(error).__name__ == 'DSHProtocolError' or isinstance(error, json.JSONDecodeError)
            or any(x in text for x in MODEL_FORMAT)):
        return 'model_format'
    if any(x in text for x in ('certificate', 'ssl_cert', 'unable to get local issuer', 'tls handshake')):
        return 'certificate'
    if any(x in text for x in ('rate limit', 'rate_limit', 'too many requests', 'http 429')):
        return 'rate_limit'
    if any(x in text for x in ('stream closed', 'service unavailable', 'provider unavailable', 'bad gateway', 'http 502', 'http 503', 'http 504', 'overloaded', 'hermes did not complete', 'opencode did not complete', 'dsh did not complete')):
        return 'service_unavailable'
    if isinstance(error, subprocess.TimeoutExpired):
        return 'timeout'
    # Package resolution conflicts need a corrected setup recipe. Repeating the
    # same install as a network retry can exhaust recovery without changing it.
    if any(x in text for x in ('eresolve', 'could not resolve dependency', 'conflicting peer dependency',
                               'unable to resolve dependency tree', 'resolutionimpossible')):
        return 'dependency_conflict'
    if isinstance(error, (ConnectionError, TimeoutError)) or any(x in text for x in ('connection reset', 'connection refused', 'timed out', 'could not resolve host', 'could not resolve hostname', 'could not resolve proxy', 'name or service not known', 'temporary failure in name resolution', 'getaddrinfo', 'enotfound', 'eai_again', 'network is unreachable', 'network unavailable', 'http/2 stream', 'econnreset', ': eof', 'unexpected eof')):
        return 'network'
    if any(x in text for x in ('evidence', 'quote', 'duplicate search incomplete', 'unread path', 'insufficient')):
        return 'insufficient_evidence'
    return 'patch_error'


def retry_ready(data, stage, at=None):
    if data.get('setup_failure', {}).get('exhausted'):
        return False
    record = data.get('infrastructure_failures', {}).get(stage, {})
    if record.get('exhausted'):
        return False
    if not record.get('retry_after'):
        return True
    return (at or dt.datetime.now(dt.timezone.utc)) >= dt.datetime.fromisoformat(record['retry_after'])


def record_setup_failure(data, stage, error, options, at=None):
    """Re-plan broken setup separately from patch quality, with a lifetime cap."""
    if type(error).__name__ != 'SetupError':
        return False
    at = at or dt.datetime.now(dt.timezone.utc)
    history = data.setdefault('setup_failure_history', [])
    history.append({'at': at.isoformat(), 'stage': stage, 'error': str(error),
                    'domain': error.domain, 'context': copy.deepcopy(error.context)})
    limit = options.get('max_setup_attempts', options.get('max_stage_attempts', 3))
    count = data.get('setup_failure', {}).get('count', 0) + 1
    data['setup_failure'] = {'count': count, 'exhausted': count >= limit,
                             'context': copy.deepcopy(error.context)}
    data['failure_category'] = 'setup_error'
    data['last_error'] = str(error)
    data['stop_reason'] = 'setup_replan_exhausted' if count >= limit else 'setup_recipe_replan'
    data['stage'] = 'PLAN'
    return True


def recover_setup_change(data, workspace, options, at=None):
    """Bounded recovery only after recorded setup inputs materially change.

    Old timeout records without an input receipt get a baseline, never a free
    retry. Call again after correcting the recipe or repairing the toolchain.
    A changed baseline still returns to PLAN for normal recipe review.
    """
    from recipe_runtime import setup_context, validate_setup, SetupError
    spec = data.get('plan', {}).get('spec')
    if not isinstance(spec, dict) or data.get('stage') not in ('PLAN', 'REPRODUCE'):
        return None
    legacy = data.get('infrastructure_failures', {}).get('REPRODUCE', {})
    current_failure = data.get('setup_failure', {})
    if not (current_failure.get('exhausted') or
            (legacy.get('exhausted') and legacy.get('category') == 'timeout')):
        return None
    context = setup_context(spec, workspace)
    prior = current_failure.get('context') or data.get('setup_recovery_baseline') or data.get('setup_context')
    if not prior or not prior.get('fingerprint'):
        data['setup_recovery_baseline'] = context
        return 'baseline_recorded'
    if context['fingerprint'] == prior['fingerprint']:
        return None
    history = data.get('setup_recoveries', [])
    if len(history) >= options.get('max_setup_recoveries', 1):
        return None
    try:
        validate_setup(spec, workspace)
    except SetupError:
        return None
    at = at or dt.datetime.now(dt.timezone.utc)
    data.setdefault('setup_recoveries', []).append({
        'at': at.isoformat(), 'previous': copy.deepcopy(prior), 'current': context,
        'failure': copy.deepcopy(current_failure or legacy), 'error': data.get('last_error'),
        'reason': 'Changed setup recipe, dependency manifests or runtime files; normal planning review required'})
    data.get('infrastructure_failures', {}).pop('REPRODUCE', None)
    data['setup_failure'] = {'count': 0, 'exhausted': False, 'context': context}
    data['setup_context'] = context
    data['stage'] = 'PLAN'
    data['stop_reason'] = 'setup_changed_replan'
    return 'replan'


def recover_dependency_setup(data, options, at=None):
    """Reclassify only the historical npm setup/DNS collision, retaining evidence.

    Repeating the same install was not three different recipe failures. Charge
    one failed recipe and re-plan within the existing stage-attempt limit.
    Service outages and all unrelated exhausted retries remain untouched.
    """
    stage = data.get('stage')
    record = data.get('infrastructure_failures', {}).get(stage, {})
    error = data.get('last_error', '')
    if (stage != 'REPRODUCE' or record.get('category') != 'network'
            or not error.startswith('Baseline setup failed; not a reproduction:')
            or category(RuntimeError(error)) != 'dependency_conflict'):
        return False
    at = at or dt.datetime.now(dt.timezone.utc)
    data.setdefault('failure_reclassifications', []).append({
        'at': at.isoformat(), 'stage': stage, 'previous': dict(record),
        'error': error, 'category': 'dependency_conflict',
        'reason': 'Dependency resolution was incorrectly classified as DNS failure',
    })
    del data['infrastructure_failures'][stage]
    count = data.setdefault('attempts', {}).get(stage, 0) + 1
    data['attempts'][stage] = count
    data['stage'] = 'FAILED' if count >= options.get('max_stage_attempts', 3) else 'PLAN'
    data['failure_category'] = 'dependency_conflict'
    data['stop_reason'] = 'dependency_recipe_attempts_exhausted' if data['stage'] == 'FAILED' else 'dependency_recipe_replan'
    return True


def recover_model_service(db, data, task_id, options, at=None):
    """Reopen one exhausted model-service retry window after proven recovery.

    Read model-call receipts without reserving or refunding calls. The caller
    persists the changed task; quality attempts and all lifetime calls remain.
    """
    stage = data.get('stage')
    purpose = {'PLAN': 'plan', 'REPAIR': 'repair'}.get(stage)
    record = data.get('infrastructure_failures', {}).get(stage, {})
    error = data.get('last_error', '')
    history = data.get('model_service_recoveries', [])
    limit = options.get('max_infrastructure_attempts', 3)
    channel = next((channel for client, channel in (('OpenCode', 'opencode'), ('DSH', 'dsh'))
                    if isinstance(error, str) and error.startswith(f'{client} did not complete {purpose}:')), None)
    if (not purpose or channel is None
            or category(RuntimeError(error)) != 'service_unavailable'
            or data.get('failure_category') != 'service_unavailable'
            or record.get('category') != 'service_unavailable'
            or record.get('exhausted') is not True
            or type(limit) is not int or limit <= 0
            or type(record.get('count')) is not int or record['count'] < limit
            or data.get('attempts', {}).get(stage, 0) >= options.get('max_stage_attempts', 3)
            or not isinstance(history, list)
            or any(not isinstance(item, dict) or item.get('stage') == stage for item in history)):
        return False

    def timestamp(value):
        try:
            parsed = dt.datetime.fromisoformat(value)
            return parsed if parsed.utcoffset() is not None else None
        except (TypeError, ValueError):
            return None

    def one(sql, params):
        cursor = db.execute(sql, params)
        row = cursor.fetchone()
        return dict(zip((column[0] for column in cursor.description), row)) if row else None

    at = at or dt.datetime.now(dt.timezone.utc)
    recorded = timestamp(record.get('at'))
    if at.utcoffset() is None or recorded is None or recorded > at:
        return False
    failed = one('SELECT * FROM model_calls WHERE task=? AND purpose=? '
                 'ORDER BY started DESC,call_id DESC LIMIT 1', (task_id, purpose))
    if (not failed or failed['channel'] != channel or not failed['model']
            or failed['outcome'] != 'service_unavailable'):
        return False
    started, ended = timestamp(failed['started']), timestamp(failed['ended'])
    # Telemetry completes the model call immediately before record_failure.
    # A different/old failure cannot unlock the current exhausted record.
    if (started is None or ended is None or started > ended
            or not 0 <= (recorded - ended).total_seconds() <= 5):
        return False
    recent = at - dt.timedelta(hours=24)
    success = one("SELECT * FROM model_calls WHERE channel=? AND model=? AND purpose=? "
                  "AND outcome='success' AND ended IS NOT NULL "
                  'AND julianday(started)>julianday(?) '
                  'AND julianday(ended)>=julianday(?) AND julianday(ended)<=julianday(?) '
                  'ORDER BY ended DESC,call_id DESC LIMIT 1',
                  (failed['channel'], failed['model'], purpose,
                   ended.isoformat(), recent.isoformat(), at.isoformat()))
    if not success:
        return False
    success_start, success_end = timestamp(success['started']), timestamp(success['ended'])
    if (success_start is None or success_end is None
            or not ended < success_start <= success_end or not recent <= success_end <= at):
        return False

    data.setdefault('model_service_recoveries', []).append({
        'at': at.isoformat(), 'task': task_id, 'stage': stage,
        'previous': copy.deepcopy(record), 'error': error,
        'failed_call': failed, 'success_call': success,
        'reason': 'Same model/channel/purpose completed successfully after this outage within 24 hours',
    })
    del data['infrastructure_failures'][stage]
    data['stop_reason'] = 'model_service_recovered_once'
    return True


def record_failure(data, stage, error, options, at=None):
    kind = category(error)
    data['failure_category'] = kind
    data['last_error'] = str(error)
    if kind not in TRANSIENT:
        return False
    at = at or dt.datetime.now(dt.timezone.utc)
    records = data.setdefault('infrastructure_failures', {})
    old = records.get(stage, {})
    count = old.get('count', 0) + 1
    limit = options.get('max_infrastructure_attempts', 3)
    delay = min(options.get('infrastructure_cooldown_seconds', 300) * 2 ** (count - 1), 3600)
    records[stage] = {'category': kind, 'count': count, 'at': at.isoformat(),
                      'retry_after': (at + dt.timedelta(seconds=delay)).isoformat(), 'exhausted': count >= limit}
    data['stop_reason'] = 'infrastructure_retry_exhausted' if count >= limit else 'infrastructure_cooldown'
    return True
