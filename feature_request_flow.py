"""Bounded live admission for existing feature issues; never claims or publishes."""
import hashlib
import json
import re

from contribute import ContributionPaused, gh, now, save
from feature_requests import evaluate, is_feature_request, scope_digest
from repository_policy import approval_receipt, human_maintainer_comments
from telemetry import ACTIVE


def options(config):
    value = config.get('feature_requests', {})
    if not isinstance(value, dict) or type(value.get('enabled', False)) is not bool:
        raise ValueError('feature_requests.enabled must be boolean')
    limit = value.get('candidates_per_tick', 2)
    if type(limit) is not int or not 0 <= limit <= 8:
        raise ValueError('feature_requests.candidates_per_tick must be 0..8')
    return value.get('enabled', False), limit


def feature_task(config, task, data=None):
    if task.get('source') != 'issue' or not task.get('number'):
        return False
    payload = json.loads(task['payload']) if isinstance(task.get('payload'), str) else task.get('payload', {})
    return is_feature_request(payload) or bool((data or {}).get('feature_request'))


def legacy_candidate(config, task):
    """Preview only; do not revive failures, other holds or terminal history."""
    return (options(config)[0] and task['state'] == 'HOLD' and json.loads(task['holds']) == ['feature needs maintainer approval']
            and feature_task(config, task))


def observe(engine, task, data, issue):
    """A live reclassification must not inherit the cached bug fast path."""
    if (task.get('source') == 'issue' and task.get('number') and not data.get('pr_number')
            and is_feature_request(issue) and not feature_task(engine.p.config, task, data)):
        data['feature_request'] = {'detected_from': 'live_issue', 'observed_at': now()}
        engine.store(task['id'], data)


def rotate(engine, rows):
    """Advance within each repository even when its previous issue was blocked."""
    buckets = {}
    for row in rows:
        buckets.setdefault(row['repo'], []).append(row)
    ordered = []
    for repo, bucket in buckets.items():
        cursor = engine.db.execute('SELECT last_repo FROM scheduler WHERE name=?',
                                   ('feature-requests:' + repo,)).fetchone()
        ids = [r['id'] for r in bucket]
        if cursor and cursor['last_repo'] in ids:
            start = ids.index(cursor['last_repo']) + 1
            bucket = bucket[start:] + bucket[:start]
        ordered.extend(bucket)
    return ordered


def turn(engine, task):
    with engine.db:
        for name, value in (('feature-requests', task['repo']),
                            ('feature-requests:' + task['repo'], task['id'])):
            engine.db.execute('INSERT INTO scheduler VALUES (?,?) ON CONFLICT(name) '
                              'DO UPDATE SET last_repo=excluded.last_repo', (name, value))


def pages(endpoint, records=None):
    """At most 300 records; a full final page remains unknown, never empty."""
    records = [] if records is None else records
    for page in range(1, 4):
        batch = gh('api', f'{endpoint}?per_page=100&page={page}')
        if not isinstance(batch, list) or not all(isinstance(item, dict) for item in batch):
            raise ValueError('Malformed feature discussion page')
        records.extend(batch)
        if len(batch) < 100:
            return records, True
    return records, False


def collect(engine, task):
    p, repo, number = engine.p, task['repo'], task['number']
    snapshot = {'repo': repo, 'observed_at': now(), 'issue': None, 'comments': [], 'linked_prs': [],
                'complete': {'issue': False, 'comments': False, 'linked_prs': False, 'policy': False}}
    folder = engine.packet_dir(task['id'])
    try:
        # Publication uses this again immediately before its write; scans and
        # stored admission decisions cannot authorize a feature implementation.
        p.require_contribution(task, refresh=True)
        rules = p._repository_evidence.get(repo, ({}, None))[1]
        snapshot['policy_rules'] = rules
        snapshot['complete']['policy'] = isinstance(rules, str) and bool(rules.strip())
        configured = p.config.get('repository_checks', {}).get('policies', {}).get(repo, {})
        snapshot['ready_project_verified'] = bool(configured.get('ready_project'))
        snapshot['issue'] = gh('api', f'repos/{repo}/issues/{number}')
        issue = snapshot['issue']
        snapshot['complete']['issue'] = (isinstance(issue, dict) and issue.get('number') == number
            and str(issue.get('html_url', '')).casefold() == f'https://github.com/{repo}/issues/{number}'.casefold())
        if not snapshot['complete']['issue']:
            raise ValueError('Feature issue identity does not match task')
        _, snapshot['complete']['comments'] = pages(f'repos/{repo}/issues/{number}/comments', snapshot['comments'])
        snapshot['timeline'] = []
        timeline, complete = pages(f'repos/{repo}/issues/{number}/timeline', snapshot['timeline'])
        linked = set()
        for event in timeline:
            if event.get('event') != 'cross-referenced':
                continue
            source = event.get('source', {}).get('issue')
            if not isinstance(source, dict):
                complete = False
                continue
            if source.get('pull_request'):
                url = source['pull_request'].get('url', '')
                match = re.fullmatch(r'https://api\.github\.com/repos/([\w.-]+/[\w.-]+)/pulls/(\d+)', url)
                if not match:
                    complete = False
                    continue
                linked.add((match[1], int(match[2])))
        if len(linked) > 10:
            complete = False
        for linked_repo, pr_number in sorted(linked)[:10]:
            pr = gh('api', f'repos/{linked_repo}/pulls/{pr_number}')
            if not isinstance(pr, dict) or pr.get('number') != pr_number:
                raise ValueError('Linked feature PR identity is invalid')
            snapshot['linked_prs'].append(pr)
        snapshot['complete']['linked_prs'] = complete
        snapshot['actor'] = gh('api', 'user')['login']
        # A collection that took longer than the freshness allowance must not
        # silently get a fresh timestamp at its end.
        return snapshot
    finally:
        save(folder / 'feature-request-context.json', snapshot)


def check(engine, task_id, data, phase='plan'):
    """Fresh discussion/policy -> reviewed scope -> normal execution gates."""
    task = engine.p.task(task_id)
    if not feature_task(engine.p.config, task, data) or data.get('pr_number'):
        return None
    if not options(engine.p.config)[0]:
        raise ContributionPaused('Feature-request delivery is disabled')
    if phase == 'plan':
        budget = ACTIVE.get().task_budget if ACTIVE.get() else getattr(engine, '_task_budget', None)
        if budget is not None:
            attempts = getattr(budget, 'feature_requests', set())
            if task_id not in attempts:
                if len(attempts) >= options(engine.p.config)[1] or not budget.admit(task_id, 'implementation'):
                    raise ContributionPaused('Feature-request admission budget is exhausted for this run')
                attempts.add(task_id)
                budget.feature_requests = attempts
                turn(engine, task)
    try:
        snapshot = collect(engine, task)
    except Exception as error:
        # Preserve the transport failure for normal retry accounting. Partial
        # evidence is never reinterpreted as an empty or eligible discussion.
        save(engine.packet_dir(task_id) / 'feature-request-admission.json',
             {'status': 'unknown', 'eligible': False, 'permits_publication': False,
              'observed_at': now(), 'reasons': [type(error).__name__ + ': ' + str(error)]})
        raise
    configured = dict(engine.p.config.get('repository_checks', {}).get('policies', {}).get(task['repo'], {}))
    if data.get('upstream_direction_required'):
        configured['feature_approval_required'] = True
    def assess():
        return evaluate(snapshot, configured, actor=snapshot['actor'], now=now(), stage=phase,
                        approval_data=data)
    verdict = assess()
    save(engine.packet_dir(task_id) / 'feature-request-admission.json',
         {'status': 'unknown', 'eligible': False, 'permits_publication': False,
          'observed_at': snapshot['observed_at'], 'reasons': ['Current scope review pending or interrupted']})
    if phase in ('repair', 'publish') and data.get('feature_plan_scope_digest') != scope_digest(snapshot):
        verdict.update(status='blocked', eligible=False)
        verdict['reasons'].append('Feature scope differs from the acceptance-test plan; replan and revalidate')
    humans = human_maintainer_comments(snapshot['comments'])
    review_key = hashlib.sha256(json.dumps({'scope': scope_digest(snapshot), 'comments': snapshot['comments'],
        'labels': snapshot['issue'].get('labels'), 'assignees': snapshot['issue'].get('assignees'),
        'linked_prs': [{key: pr.get(key) for key in ('html_url', 'number', 'state', 'merged', 'merged_at',
                                                   'title', 'body', 'head')} for pr in snapshot['linked_prs']],
        'acceptance_plan': data.get('plan') if phase != 'plan' else None,
        'rules': snapshot['policy_rules'], 'policy': configured}, sort_keys=True).encode()).hexdigest()
    approval_reason = 'Upstream requires current human maintainer approval of this scope'
    if (verdict['reasons'] == [approval_reason] and humans
            and data.get('feature_scope_review_key') != review_key):
        decision = engine.judge(task_id, 'feature-scope-review', {
            'gate': 'Do these verified human maintainer comments explicitly approve implementing the exact current '
                    'feature request? Reject mere interest, labels, silence, approval of another scope, rejection '
                    'or unresolved design discussion. Cite the actual comment and approved scope in your reason.',
            'issue': snapshot['issue'], 'comments': humans, 'rules': snapshot['policy_rules']})
        data['feature_scope_review_key'] = review_key
        if decision['approved']:
            data['maintainer_approval'] = approval_receipt(humans, decision, now())
            data['feature_request_scope_digest'] = scope_digest(snapshot)
        engine.store(task_id, data)
        verdict = assess()
    # Even repositories without a mandatory approval rule may have rejected
    # this particular proposal. Review the live discussion before spending code slots.
    if verdict['eligible']:
        if data.get('feature_request_review_key') != review_key:
            decision = engine.judge(task_id, 'feature-request-review', {
                'gate': 'Is this existing feature request sufficiently clear, bounded and valuable to attempt '
                        'acceptance-test planning? Check the full discussion for maintainer rejection, deferred '
                        'design, unresolved competing claims (including non-English text), existing implementation, '
                        'and explicit repository assignment/claim/approval or automation restrictions. Unassigned '
                        'does not mean approved or claimed. Return false when uncertain. No permission or claim '
                        'is created by your verdict. If acceptance_plan is supplied, confirm its exact scope and '
                        'acceptance criteria still match the current request and full discussion, especially newer '
                        'maintainer direction. Reject stale or expanded plans. Source implementation and real tests are checked next.',
                'issue': snapshot['issue'], 'comments': snapshot['comments'], 'linked_prs': snapshot['linked_prs'],
                'acceptance_plan': data.get('plan') if phase != 'plan' else None,
                'rules': snapshot['policy_rules'], 'controller_admission': verdict})
            data['feature_request_review_key'] = review_key
            data['feature_request_review'] = decision
        if not data.get('feature_request_review', {}).get('approved'):
            verdict.update(status='blocked', eligible=False)
            verdict['reasons'].append('Feature scope review: ' + str(data.get('feature_request_review', {}).get('reason')))
    # Review can outlast the evidence freshness window. Never turn that old
    # snapshot into a fresh permit simply because the reviewer eventually replied.
    final_assessment = assess()
    if not final_assessment['eligible']:
        verdict.update(eligible=False, status=final_assessment['status'],
                       unknown_reasons=final_assessment['unknown_reasons'])
        verdict['reasons'] += [reason for reason in final_assessment['reasons'] if reason not in verdict['reasons']]
    verdict['scope_approval_current'] = final_assessment['scope_approval_current']
    data['maintainer_approved'] = verdict['scope_approval_current']
    data['feature_request'] = {'issue_url': snapshot['issue'].get('html_url'),
                               'scope_digest': scope_digest(snapshot), **verdict}
    engine.store(task_id, data)
    save(engine.packet_dir(task_id) / 'feature-request-admission.json', data['feature_request'])
    if not verdict['eligible']:
        raise ContributionPaused('; '.join(verdict['reasons']))
    # Only a freshly eligible pending task can leave the historical blanket hold.
    holds = json.loads(task['holds'])
    if task['state'] == 'HOLD' and holds == ['feature needs maintainer approval']:
        with engine.db:
            engine.db.execute("UPDATE tasks SET state='DISCOVERED',holds='[]',updated=? WHERE id=?", (now(), task_id))
        engine.p.event(task_id, 'Replaced blanket feature hold with fresh feature-request admission; no claim or publication performed')
    return {**snapshot['issue'], 'comments_data': snapshot['comments']}
