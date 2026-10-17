"""Evidence-backed cross-harness matrices; SQLite is canonical, JSON is an export."""
import hashlib
import json
from pathlib import PurePosixPath
import re
from urllib.parse import quote

from telemetry import utc

CATEGORIES = ('context_history', 'session_recovery', 'tools', 'permissions_sandbox',
              'mcp_skills_hooks', 'multi_agent', 'model_providers', 'planning',
              'cancellation_resources', 'git_review_testing', 'errors_retries',
              'terminal_observability', 'plugins_configuration', 'security_publication')
STATUSES = ('implemented', 'partial', 'not_found', 'unknown')
QUEUES = ('READY_TO_IMPLEMENT', 'NEEDS_MAINTAINER_CONFIRMATION', 'RESEARCH_ONLY', 'PAUSED_POLICY')


def file_type(path):
    name = path.lower()
    if PurePosixPath(name).suffix in ('.md', '.rst', '.txt', '.mdx'):
        return 'documentation'
    if PurePosixPath(name).suffix in ('.py', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.go', '.rs', '.java', '.cs', '.c', '.cpp', '.sh', '.rb', '.kt', '.swift'):
        basename = PurePosixPath(name).name
        helper = any(word in basename for word in ('__init__', 'conftest', 'test-setup', 'test-helper', 'test-utils'))
        if not helper and (re.search(r'(^|[/])(?:tests?|specs?)/', name) or
                           re.search(r'(^|[_.-])tests?([_.-]|$)|\.(?:test|spec)\.', basename)):
            return 'test'
        return 'source'
    return 'configuration'


def evidence(item, sources, feature_id):
    if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k].strip() for k in ('repo', 'path', 'quote')):
        raise ValueError('Invalid matrix evidence')
    source = sources.get(item['repo'], {})
    text = source.get('files', {}).get(item['path'])
    if not text or item['quote'] not in text or not source.get('sha'):
        raise ValueError('Matrix quote does not match pinned source: ' + item['path'])
    if item.get('sha', source['sha']) != source['sha']:
        raise ValueError('Matrix evidence SHA mismatch')
    return {**item, 'sha': source['sha'], 'file_type': file_type(item['path']), 'feature_id': feature_id,
            'url': f"https://github.com/{item['repo']}/blob/{source['sha']}/{quote(item['path'], safe='/')}"}


def build(answer, task, packet):
    raw = answer.get('feature_matrix')
    if not isinstance(raw, dict):
        raise ValueError('Version 2 research requires feature_matrix')
    key = raw.get('feature_key', '')
    if not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9-]{2,79}', key):
        raise ValueError('Stable kebab-case feature_key required')
    if raw.get('category') not in CATEGORIES:
        raise ValueError('Unknown feature taxonomy category')
    identifier = hashlib.sha256((task['repo'].lower() + ':' + key).encode()).hexdigest()[:24]
    fields = ('feature_name', 'user_scenario', 'gap_analysis', 'migration_analysis', 'risks')
    for field in fields:
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise ValueError('Missing matrix field: ' + field)
    for field in ('affected_files', 'implementation_plan', 'validation_plan'):
        if (not isinstance(raw.get(field), list) or not raw[field] or len(raw[field]) > 20
                or any(not isinstance(x, str) or not x.strip() for x in raw[field])):
            raise ValueError('Missing bounded matrix plan: ' + field)
    for path in raw['affected_files']:
        if PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or '\\' in path or ':' in path:
            raise ValueError('Affected files must be repository relative')
    if packet['target']['repo'] != task['repo'] or packet['target']['sha'] != task['base_sha']:
        raise ValueError('Target snapshot does not match task baseline')
    supplied = [packet['target'], *packet['peers']]
    if len({s['repo'].lower() for s in supplied}) != len(supplied) or len(packet['peers']) > 5:
        raise ValueError('Matrix requires distinct repositories and at most five peers')
    for source in supplied:
        sha = source.get('sha')
        if sha is not None and (not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{40}', sha)):
            raise ValueError('Evidence version must be a full immutable SHA')
        if not sha and not source.get('error'):
            raise ValueError('Successful snapshot requires a fixed SHA')
    sources = {s['repo']: s for s in supplied}
    # Older prose prompts did not specify nesting; accept their top-level map
    # without weakening any evidence checks. Ambiguous conflicting maps fail closed.
    statuses = raw.get('feature_status', answer.get('feature_status'))
    if 'feature_status' in raw and 'feature_status' in answer and raw['feature_status'] != answer['feature_status']:
        raise ValueError('Conflicting feature_status maps')
    if not isinstance(statuses, dict) or set(statuses) != set(sources):
        raise ValueError('Matrix must cover target and every selected peer exactly once')
    normalized, all_evidence = {}, []
    for repo, source in sources.items():
        status = statuses[repo]
        if not isinstance(status, dict) or status.get('status') not in STATUSES or not isinstance(status.get('reason'), str) or not status['reason'].strip():
            raise ValueError('Invalid feature status/reason')
        claims = status.get('evidence', [])
        if not isinstance(claims, list) or len(claims) > 8 or any(not isinstance(e, dict) or e.get('repo') != repo for e in claims):
            raise ValueError('Target and peer evidence cannot be mixed')
        checked = [evidence(e, sources, identifier) for e in claims]
        scope = status.get('checked_paths', [])
        if not isinstance(scope, list) or any(not isinstance(p, str) or p not in source.get('files', {}) for p in scope):
            raise ValueError('Inspection scope must contain actually read files')
        state = status['status']
        if source.get('error') and state != 'unknown':
            raise ValueError('Failed peer inspection must remain unknown')
        if state in ('implemented', 'partial') and not any(e['file_type'] in ('source', 'test') for e in checked):
            raise ValueError('Implementation requires source/test evidence, not README claims')
        if state == 'partial' and not status.get('missing'):
            raise ValueError('Partial implementation must explain missing behavior')
        if state == 'not_found' and not any(file_type(p) in ('source', 'test') for p in scope):
            raise ValueError('not_found requires an explicit inspected code/test scope')
        normalized[repo] = {**status, 'sha': source.get('sha'), 'evidence': checked,
                            'checked_paths': scope, 'scope_limited': state == 'not_found'}
        all_evidence.extend(checked)
    stamp = utc()
    tests = [{**e, 'execution_result': 'not_run'} for e in all_evidence if e['file_type'] == 'test']
    return {**{k: raw[k] for k in (*fields, 'affected_files', 'implementation_plan', 'validation_plan', 'category')},
            'schema_version': 1, 'feature_id': identifier, 'feature_key': key,
            'target_repo': task['repo'], 'target_sha': task['base_sha'],
            'peer_repositories': [{'repo': s['repo'], 'sha': s.get('sha'), 'error': s.get('error')} for s in packet['peers']],
            'feature_status': normalized, 'evidence': all_evidence, 'tests': tests,
            'contribution_path': 'research_only', 'created_at': stamp, 'updated_at': stamp}


def route(answer, matrix, *, reviewed=False, duplicate_clear=False, maintainer=False, policy=None):
    policy = policy or {}
    if answer.get('category') == 'security' or answer.get('decision') != 'candidate':
        return 'RESEARCH_ONLY', 0, 'private_or_no_candidate'
    allowed = answer.get('policy', {})
    if (not allowed.get('automated_pr') or policy.get('human_pr_review_required')
            or policy.get('manual_issue_submission') or policy.get('research_only')):
        return 'PAUSED_POLICY', 0, 'automation_or_personal_review_policy'
    if not reviewed or not duplicate_clear:
        return 'RESEARCH_ONLY', 0, 'review_or_duplicate_gate_pending'
    if matrix:
        target = matrix['feature_status'][matrix['target_repo']]['status']
        peers = [v for k, v in matrix['feature_status'].items() if k != matrix['target_repo']]
        if len(peers) < 3 or target not in ('not_found', 'partial') or not any(p['status'] == 'implemented' for p in peers):
            return 'RESEARCH_ONLY', 0, 'insufficient_gap_or_peer_evidence'
    # Model value prose is retained for review; model numerical scores never determine admission.
    priority = (100 if answer['category'] == 'feature' else 40) + (10 if answer['risk'] == 'low' else 0)
    discussion = answer['category'] == 'feature' or answer['risk'] != 'low' or allowed.get('maintainer_required')
    if discussion and not maintainer:
        return 'NEEDS_MAINTAINER_CONFIRMATION', priority, 'design_direction_requires_maintainer'
    if answer['risk'] == 'high':
        return 'PAUSED_POLICY', 0, 'high_risk_requires_human_patch_review'
    return 'READY_TO_IMPLEMENT', priority, 'evidence_policy_review_and_duplicate_gates_passed'


def persist(engine, task, answer, matrix, **gates):
    policy = dict(engine.p.config.get('repository_checks', {}).get('policies', {}).get(task['repo'], {}))
    policy['research_only'] = task['repo'] not in engine.p.config['repositories']
    queue, priority, reason = route(answer, matrix, policy=policy, **gates)
    if matrix is None:
        # Legacy candidates keep their existing state machine and get a conservative index.
        identifier = hashlib.sha256((task['repo'] + ':' + answer.get('title', task['title'])).lower().encode()).hexdigest()[:24]
        matrix = {'feature_id': identifier, 'created_at': utc(), 'updated_at': utc(), 'legacy': True}
    identifier = matrix['feature_id']
    old = engine.db.execute('SELECT task,created_at FROM research_candidates WHERE feature_id=?', (identifier,)).fetchone()
    if old and old['task'] != task['id']:
        return 'RESEARCH_ONLY', True
    if old:
        matrix['created_at'] = old['created_at']
    matrix.update(updated_at=utc(), contribution_path={'READY_TO_IMPLEMENT': 'implement',
                  'NEEDS_MAINTAINER_CONFIRMATION': 'discuss'}.get(queue, 'research_only'), queue=queue, queue_reason=reason)
    with engine.db:
        engine.db.execute('INSERT INTO research_candidates VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(feature_id) DO UPDATE SET '
                          'category=excluded.category,queue=excluded.queue,priority=excluded.priority,matrix=excluded.matrix,updated_at=excluded.updated_at',
                          (identifier, task['id'], task['repo'], answer.get('category', 'unknown'), queue, priority,
                           json.dumps(matrix, ensure_ascii=False), matrix['created_at'], matrix['updated_at']))
    from contribute import save
    save(engine.packet_dir(task['id']) / 'feature-matrix.json', matrix)
    return queue, False


PROMPT = '''For every candidate include feature_matrix with feature_key (stable lowercase kebab-case identity),
feature_name (string), category (string, one of %s), user_scenario (string), gap_analysis (string, including existing alternatives),
migration_analysis (string, reusable design AND architectural incompatibilities; do not copy code blindly),
affected_files (relative paths array of strings), implementation_plan (steps array of strings), validation_plan (commands and acceptance array of strings), risks (nonempty STRING, not an array).
Inside feature_matrix, the REQUIRED feature_status object maps EVERY target/peer repo to {status: implemented|partial|not_found|unknown, reason,
missing (required for partial), checked_paths: [actually supplied paths], evidence: [{repo,path,quote}]}.
implemented/partial require exact code/test quotes. not_found means only in checked_paths, never whole-project absence.
Failed/incomplete inspections are unknown. Tests are static evidence, NOT execution results.
Nesting example: {"feature_matrix":{"feature_key":"resume-session", ...,"feature_status":{"owner/target":{...},"owner/peer":{...}}}}.
''' % ', '.join(CATEGORIES)
