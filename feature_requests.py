"""Pure feature-request admission checks; no network, models or publication.

Collectors must supply fresh, complete issue, discussion, linked-PR and policy
observations. Eligibility here only admits normal planning/verification gates;
it never grants maintainer approval, a claim, or permission to publish.
"""
import datetime as dt
import hashlib
import json
import re

from repository_policy import (contribution_constraints, has_scope_approval,
                               human_maintainer_comments, issue_policy_reason)


_TYPE = r'(?:feature(?:[ -]request)?|enhancement|new[ -]feature)'
_LABEL = re.compile(r'(?:' + _TYPE + r'|(?:type|kind)\s*[:/]\s*' + _TYPE + r')', re.I)
_TITLE = re.compile(r'^\s*(?:\[' + _TYPE + r'\]\s*:?|' + _TYPE + r'\s*:|feat\s*[:(])', re.I)


def _signals(issue):
    labels = issue.get('labels') or []
    if not isinstance(labels, list):
        labels = []
    found = ['label:' + x['name'] for x in labels if isinstance(x, dict)
             and isinstance(x.get('name'), str) and _LABEL.fullmatch(x['name'].strip())]
    if isinstance(issue.get('title'), str) and _TITLE.match(issue['title']):
        found.append('title_prefix')
    return found


def is_feature_request(issue):
    return isinstance(issue, dict) and not issue.get('pull_request') and bool(_signals(issue))


def candidate_metadata(issue):
    """Labels are discovery evidence, never authorization or automatic holds."""
    assignees = issue.get('assignees')
    assignment = 'unknown'
    if isinstance(assignees, list) and all(isinstance(x, dict) and x.get('login') for x in assignees):
        assignment = 'assigned' if assignees else 'unassigned'
    return {'kind': 'feature' if is_feature_request(issue) else 'other',
            'signals': _signals(issue), 'assignment': assignment,
            'authorization': 'not_assessed'}


def constraints(rules, configured=None):
    """Add narrow mandatory rules; ambiguous prose still needs policy review.

    Recognized requirements only tighten configured gates. In particular,
    permissive labels or a sentence saying permission is unnecessary cannot
    erase a separate feature-approval requirement.
    """
    policy = contribution_constraints(rules, configured)
    plain = re.sub(r'\s+', ' ', rules or '')
    patterns = {
        'feature_approval_required': [
            r'(?:for (?:new )?features?|feature requests?|feature proposals?)[^.!?]{0,240}'
            r'(?:must|should|require[sd]?|need|make sure|has been|have been|get)[^.!?]{0,180}'
            r'(?:approv(?:ed|al)|accept(?:ed|ance))',
            r'(?:approv(?:ed|al)|accept(?:ed|ance))[^.!?]{0,120}'
            r'(?:before|prior to)[^.!?]{0,100}(?:implement|cod(?:e|ing)|work|pull request|\bPR\b)',
        ],
        'discussion_required': [
            r'(?:for (?:new )?features?|feature requests?)[^.!?]{0,180}'
            r'(?:must|should|first|please|open a|start a)[^.!?]{0,80}discuss(?:ion)?',
            r'(?:must|please|always)\s+discuss[^.!?]{0,120}'
            r'(?:before|prior to)[^.!?]{0,80}(?:implement|cod(?:e|ing)|work|\bPR\b|pull request)',
        ],
        'assignment_required': [
            r'(?:must|need to|required to|please|always)[^.!?]{0,60}(?:be assigned|assign yourself|assigned to you)'
            r'[^.!?]{0,100}(?:before|prior to|first)',
            r'(?:before|prior to)[^.!?]{0,80}(?:work|implement|cod(?:e|ing))'
            r'[^.!?]{0,100}(?:must|please|ensure|make sure)[^.!?]{0,50}(?:assigned|assign yourself)',
        ],
        'claim_required': [
            r'(?:must|need to|required to|please|always)[^.!?]{0,40}claim[^.!?]{0,80}'
            r'(?:before|prior to|first)',
            r'(?:before|prior to)[^.!?]{0,80}(?:work|implement|cod(?:e|ing))'
            r'[^.!?]{0,80}(?:must|please|ensure|make sure)[^.!?]{0,40}claim',
        ],
    }
    clauses = re.split(r'[.!?;]|\bbut\b|\bhowever\b', plain, flags=re.I)
    no_approval = re.compile(
        r"(?:do(?:es)? not|don't|doesn't)\s+(?:need|require)[^;.!?]{0,40}(?:approv|permission)"
        r'|(?:approval|permission)\s+(?:is|are)\s+not\s+(?:needed|required)'
        r'|no\s+(?:maintainer\s+)?(?:approval|permission)\s+(?:is\s+)?(?:needed|required)', re.I)
    for name, expressions in patterns.items():
        if any(re.search(pattern, clause, re.I) for clause in clauses
               if not (name == 'feature_approval_required' and no_approval.search(clause))
               for pattern in expressions):
            policy[name] = True
    return policy


def _timestamp(value):
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    else:
        raise ValueError('Missing observation time')
    if parsed.tzinfo is None:
        raise ValueError('Observation time must include a timezone')
    return parsed.astimezone(dt.timezone.utc)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def scope_digest(snapshot):
    """Bind reviewed direction to content, not noisy assignment/update times."""
    issue = snapshot.get('issue') or {}
    return _digest({'repo': str(snapshot.get('repo', '')).casefold(),
                    'number': issue.get('number'), 'title': issue.get('title'),
                    'body': issue.get('body') or ''})


def _claims(comments):
    """Conservative explicit claim/interest signals, not a proof of no overlap.

    Old claims never expire by age. Only a later explicit release by the same
    human author clears that author's claim. Quoted examples are not claims.
    """
    active = {}
    own_text = lambda text: '\n'.join(line for line in re.sub(r'```[\s\S]*?```', '', text).splitlines()
                                      if not line.lstrip().startswith('>'))
    release = re.compile(r"\b(?:i(?:'m| am)? (?:am )?(?:no longer|not) working on|i (?:will not|won't|cannot|can't) (?:work on|take)|unassign me|i(?:'m| am) (?:dropping|releasing) (?:this|the issue)|anyone (?:else )?can (?:take|pick) (?:this|it))\b", re.I)
    claim = re.compile(r"\b(?:i(?:'m| am) working on|i(?:'ll| will| would like to| want to) (?:work on|take|pick up|implement)|(?:can|could|may) i (?:work on|take|pick up|implement)|assign (?:this (?:issue )?to )?me|i (?:have started|started) (?:working on|implementing)|i(?:'ve| have) (?:opened|submitted) (?:a |the )?(?:pr|pull request))\b", re.I)
    for item in sorted(comments, key=lambda x: (x.get('created_at', ''), x['id'])):
        user = item['user']
        if user.get('type') != 'User' or user['login'].casefold().endswith('[bot]'):
            continue
        author = user['login'].casefold()
        body = own_text(item['body'])
        if release.search(body):
            active.pop(author, None)
        elif claim.search(body):
            active[author] = {'author': user['login'], 'comment_id': item['id'],
                              'url': item.get('html_url'), 'kind': 'claim_or_interest'}
    return list(active.values())


def evaluate(snapshot, policy=None, *, actor, now, max_age_seconds=900,
             stage='plan', approval_data=None):
    """Evaluate a complete GET-only snapshot, with no side effects.

    Required snapshot fields: repo, issue, comments, linked_prs, observed_at,
    complete={issue,comments,linked_prs,policy}. The collector must resolve all
    linked PR identities/states and mark capped/failed collection incomplete.
    policy_rules is the fresh full guide; ready_project_verified is supplied
    only after the controller checks the configured project. Fresh collection
    is required before planning/repair and again immediately before publishing.
    Semantic duplicate review, real acceptance tests and patch review remain
    separate mandatory gates, even when status is eligible.
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError('Authenticated actor is required')
    if type(max_age_seconds) not in (int, float) or not 0 < max_age_seconds <= 3600:
        raise ValueError('Freshness bound must be positive and at most one hour')
    if stage not in ('plan', 'repair', 'publish'):
        raise ValueError('Unknown feature admission stage')
    actor = actor.casefold()
    reasons, unknown = [], []
    complete = snapshot.get('complete', {})
    for name in ('issue', 'comments', 'linked_prs', 'policy'):
        if not isinstance(complete, dict) or complete.get(name) is not True:
            unknown.append('Incomplete ' + name + ' observation')
    try:
        age = (_timestamp(now) - _timestamp(snapshot.get('observed_at'))).total_seconds()
        if age < 0 or age > max_age_seconds:
            unknown.append('Stale or future-dated observation')
    except (ValueError, TypeError, OverflowError):
        unknown.append('Invalid observation time')
    issue = snapshot.get('issue')
    if not isinstance(issue, dict):
        issue = {}
    repo = snapshot.get('repo')
    number = issue.get('number')
    if (not isinstance(repo, str) or not re.fullmatch(r'[\w.-]+/[\w.-]+', repo)
            or type(number) is not int or number <= 0):
        unknown.append('Unverified issue identity')
    else:
        expected = f'https://github.com/{repo}/issues/{number}'.casefold()
        if str(issue.get('html_url', issue.get('url', ''))).casefold() != expected:
            unknown.append('Issue URL does not match repository and number')
    if issue.get('pull_request') or str(issue.get('state', '')).casefold() != 'open':
        reasons.append('Feature request must be an open issue')
    if not isinstance(issue.get('title'), str) or 'body' not in issue or not isinstance(issue.get('body'), (str, type(None))):
        unknown.append('Issue scope content is unknown')
    assignees = issue.get('assignees')
    assigned = set()
    if not isinstance(assignees, list) or any(not isinstance(x, dict) or not isinstance(x.get('login'), str) or not x['login'] for x in assignees):
        unknown.append('Assignment status is unknown')
    else:
        assigned = {x['login'].casefold() for x in assignees}
        if assigned - {actor}:
            reasons.append('Issue is assigned to another contributor')
    comments = snapshot.get('comments')
    valid_comments = (isinstance(comments, list) and all(isinstance(x, dict) and type(x.get('id')) is int
        and isinstance(x.get('body'), str) and isinstance(x.get('user'), dict)
        and isinstance(x['user'].get('login'), str) and bool(x['user']['login'])
        and x['user'].get('type') in ('User', 'Bot') for x in comments))
    if not valid_comments:
        unknown.append('Discussion records are malformed')
        comments = []
    claims = _claims(comments)
    if any(x['author'].casefold() != actor for x in claims):
        reasons.append('Another contributor has an unresolved claim or implementation interest')
    own_claim = actor in assigned or any(x['author'].casefold() == actor for x in claims)
    labels = issue.get('labels')
    if not isinstance(labels, list) or any(not isinstance(x, dict) or not isinstance(x.get('name'), str) for x in labels):
        unknown.append('Issue labels are unknown')
        labels = []
    if any(x['name'].strip().casefold() in ('in progress', 'in-progress', 'status: in progress', 'claimed', 'wip') for x in labels) and not own_claim:
        reasons.append('Issue is marked as claimed or in progress')
    linked = snapshot.get('linked_prs')
    if not isinstance(linked, list):
        unknown.append('Linked PR records are unknown')
        linked = []
    for pr in linked:
        if not isinstance(pr, dict) or type(pr.get('number')) is not int or str(pr.get('state', '')).casefold() not in ('open', 'closed', 'merged'):
            unknown.append('Linked PR state is unknown')
        elif str(pr['state']).casefold() == 'open':
            reasons.append('Existing open linked PR: ' + str(pr.get('html_url', pr.get('url', pr['number']))))
        elif pr.get('merged') is True or pr.get('merged_at') or str(pr['state']).casefold() == 'merged':
            reasons.append('Linked PR was merged; implementation coverage needs review')
        elif str(pr['state']).casefold() == 'closed' and not (pr.get('merged') is False or pr.get('state') == 'CLOSED'):
            unknown.append('Closed linked PR merge status is unknown')
    current_approval = (not unknown and has_scope_approval(approval_data or {})
                        and (approval_data or {})['maintainer_approval']['digest'] == _digest(human_maintainer_comments(comments))
                        and (approval_data or {}).get('feature_request_scope_digest') == scope_digest(snapshot))
    rules = snapshot.get('policy_rules', '')
    if not isinstance(rules, str):
        unknown.append('Policy text is malformed')
        rules = ''
    effective = constraints(rules, policy)
    if reason := issue_policy_reason(issue, effective):
        reasons.append(reason)
    if effective.get('ready_project') and snapshot.get('ready_project_verified') is not True:
        reasons.append('Required project Ready status has not been verified')
    scope_required = any(effective.get(x) is True for x in ('feature_approval_required', 'maintainer_required', 'discussion_required'))
    scope_required |= effective.get('automation') == 'requires_explicit_maintainer_instructions'
    scope_required |= effective.get('pr_submission') == 'maintainer_authorization_required'
    if scope_required and not current_approval:
        reasons.append('Upstream requires current human maintainer approval of this scope')
    if effective.get('assignment_required') and actor not in assigned:
        reasons.append('Upstream requires assignment to the current contributor')
    if effective.get('claim_required') and not own_claim:
        reasons.append('Upstream requires a prior claim; none is verified')
    if effective.get('automated_pr') is False or effective.get('automation') in ('forbidden', 'disallowed'):
        reasons.append('Upstream forbids this automated contribution')
    if effective.get('manual_pr_submission'):
        reasons.append('Upstream requires personal PR submission; automatic feature delivery is disabled')
    # manual_issue_submission forbids creating new issues, not implementing an
    # already existing numbered request. This module never creates an issue.
    if stage == 'publish' and effective.get('human_pr_review_required'):
        reasons.append('Upstream requires personal code review before publication')
    status = 'blocked' if reasons else 'unknown' if unknown else 'eligible'
    return {'status': status, 'eligible': status == 'eligible', 'reasons': reasons + unknown,
            'unknown_reasons': unknown, 'claims': claims, 'scope_approval_current': bool(current_approval),
            'observed_at': snapshot.get('observed_at'), 'evidence_digest': _digest(snapshot),
            'scope_digest': scope_digest(snapshot), 'policy': effective, 'permits_publication': False}
