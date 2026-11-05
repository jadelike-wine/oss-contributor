"""Failure domains and bounded retry state; quality attempts stay separate."""
import datetime as dt
import json
import re
import subprocess

TRANSIENT = {'network', 'rate_limit', 'certificate', 'service_unavailable', 'model_format', 'timeout'}
MODEL_FORMAT = ('invalid reviewer verdict', 'model must return', 'invalid model', 'ambiguous model response',
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
    if type(error).__name__ == 'ContributionPaused':
        return 'policy'
    if isinstance(error, json.JSONDecodeError) or any(x in text for x in MODEL_FORMAT):
        return 'model_format'
    if any(x in text for x in ('certificate', 'ssl_cert', 'unable to get local issuer', 'tls handshake')):
        return 'certificate'
    if any(x in text for x in ('rate limit', 'rate_limit', 'too many requests', 'http 429')):
        return 'rate_limit'
    if any(x in text for x in ('stream closed', 'service unavailable', 'provider unavailable', 'bad gateway', 'http 502', 'http 503', 'http 504', 'overloaded', 'hermes did not complete', 'opencode did not complete')):
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
    record = data.get('infrastructure_failures', {}).get(stage, {})
    if record.get('exhausted'):
        return False
    if not record.get('retry_after'):
        return True
    return (at or dt.datetime.now(dt.timezone.utc)) >= dt.datetime.fromisoformat(record['retry_after'])


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
