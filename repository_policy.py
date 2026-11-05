"""Read-only repository health checks and upstream contribution gates (stdlib only)."""
import datetime as dt
import hashlib
import json
import re
from urllib.parse import urlsplit, quote


def contribution_constraints(rules, configured=None):
    """Add explicit machine-checkable upstream gates; never infer permission.

    This recognizes unambiguous mandatory labels and autonomous-submission bans.
    All other contribution rules still require the existing policy review.
    Configured gates and live rules are combined, so configuration cannot relax
    a requirement discovered in the official guide.
    """
    policy = dict(configured or {})
    labels = policy.get('required_issue_labels', [])
    if not isinstance(labels, list) or any(not isinstance(label, str) or not label.strip() for label in labels):
        raise ValueError('required_issue_labels must be a list of nonempty label names')
    required = {label.strip().casefold() for label in labels}
    if policy.get('ready_label'):
        if not isinstance(policy['ready_label'], str):
            raise ValueError('ready_label must be a label name')
        required.add(policy['ready_label'].strip().casefold())
    plain = re.sub(r'\s+', ' ', rules or '')
    patterns = [
        r'only start work once[^.]{0,160}?added the [`\"]([^`\"]+)[`\"] label',
        r'(?:linked )?issue is not labeled [`\"]([^`\"]+)[`\"][^.]{0,120}?closed automatically',
        r'issues? must (?:have|carry) the [`\"]([^`\"]+)[`\"] label',
    ]
    for pattern in patterns:
        required.update(match.strip().casefold() for match in re.findall(pattern, plain, flags=re.I))
    policy['required_issue_labels'] = sorted(required)
    # AI-assisted development can be welcome while agent-authored publication is
    # forbidden. Keep this separate from repository health/research admission.
    plain = re.sub(r'[*`>]', '', plain)
    evidence = []
    for match in re.finditer(r'\bdo not submit\s+([^.!?]{1,100}?)\s+from\s+fully autonomous\s+(?:ai\s+)?agents?\b',
                             plain, flags=re.I):
        kinds = match.group(1)
        found = False
        for pattern, field in ((r'\bissues?\b', 'manual_issue_submission'),
                               (r'\b(?:prs?|pull requests?)\b', 'manual_pr_submission'),
                               (r'\breviews?\b', 'manual_review_submission')):
            if re.search(pattern, kinds, flags=re.I):
                policy[field] = True
                found = True
        if found:
            evidence.append(match.group(0))
    direct = re.search(r'\bif you are an? ai agent\b[^.!?]{0,100}\b(?:do not|must not)\s+'
                       r'(?:open|submit|create)\s+(?:prs?|pull requests?)\b', plain, flags=re.I)
    if direct:
        policy['manual_pr_submission'] = True
        evidence.append(direct.group(0))
    autonomous_pr = re.search(r'\bif you have an? agent filing (?:prs?|pull requests?)\b'
                             r'[^.!?]{0,120}\bautonomously\b[^.!?]{0,60}'
                             r'\b(?:please )?turn it off for this repository\b', plain, flags=re.I)
    if autonomous_pr:
        policy['manual_pr_submission'] = True
        evidence.append(autonomous_pr.group(0))
    if evidence:
        policy['automatic_submission_reason'] = 'Upstream prohibits autonomous AI submission; human submission required'
        policy['automatic_submission_evidence'] = list(dict.fromkeys(evidence))
    return policy


def issue_policy_reason(issue, policy):
    """Return an unsatisfied hard gate, independent of model/maintainer claims."""
    required = policy.get('required_issue_labels') or []
    if not required:
        return None
    if not issue or issue.get('pull_request') or str(issue.get('state', '')).lower() != 'open':
        return 'Upstream requires an OPEN issue with labels: ' + ', '.join(required)
    labels = issue.get('labels')
    if not isinstance(labels, list) or any(not isinstance(label, dict) or not isinstance(label.get('name'), str) for label in labels):
        return 'Cannot verify required issue labels: ' + ', '.join(required)
    missing = set(required) - {label['name'].casefold() for label in labels}
    return 'Upstream requires issue labels: ' + ', '.join(sorted(missing)) if missing else None


def human_maintainer_comments(comments):
    """Association plus a verified human account is required for scope approval."""
    result = []
    for comment in comments:
        user = comment.get('user') or {}
        login = user.get('login')
        if (comment.get('author_association') in ('OWNER', 'MEMBER', 'COLLABORATOR')
                and user.get('type') == 'User' and isinstance(login, str) and login
                and not login.casefold().endswith('[bot]') and comment.get('body')
                and type(comment.get('id')) is int):
            result.append(comment)
    return result


def approval_receipt(comments, verdict, at):
    """Record interpreted scope approval separately from formal PR approval."""
    humans = human_maintainer_comments(comments)
    if not humans or verdict.get('approved') is not True:
        raise ValueError('Scope approval requires reviewed human maintainer comments')
    return {'source': 'human_comment_review', 'approved': True,
            'comment_ids': sorted({c['id'] for c in humans}),
            'digest': hashlib.sha256(json.dumps(humans, sort_keys=True).encode()).hexdigest(),
            'reviewed_at': at, 'reason': verdict.get('reason'),
            'review_identity': verdict.get('review_identity')}


def has_scope_approval(data):
    """Legacy booleans, labels and elapsed time cannot certify human approval."""
    receipt = data.get('maintainer_approval')
    if not isinstance(receipt, dict):
        return False
    ids = receipt.get('comment_ids')
    digest = receipt.get('digest')
    return (receipt.get('source') == 'human_comment_review' and receipt.get('approved') is True
            and isinstance(ids, list) and bool(ids) and all(type(i) is int for i in ids)
            and isinstance(digest, str) and bool(re.fullmatch('[0-9a-f]{64}', digest))
            and isinstance(receipt.get('reviewed_at'), str) and bool(receipt['reviewed_at']))


def contributing_guide(repo, gh, command):
    endpoint = f'repos/{repo}/contents/CONTRIBUTING.md'
    try:
        return command(['gh', 'api', endpoint, '-H', 'Accept: application/vnd.github.raw+json']), endpoint
    except RuntimeError as error:
        if not re.search(r'\bHTTP 404\b', str(error)):
            raise
    profile = gh('api', f'repos/{repo}/community/profile')
    if not isinstance(profile, dict) or not isinstance(profile.get('files'), dict):
        raise ValueError('Invalid community profile')
    guide = profile['files'].get('contributing')
    if not guide:
        return None, None
    url = guide['url']
    if not isinstance(url, str) or not url.lower().startswith(f'https://api.github.com/repos/{repo}/contents/'.lower()):
        raise ValueError('Invalid contributing guide URL')
    try:
        return command(['gh', 'api', url, '-H', 'Accept: application/vnd.github.raw+json']), url
    except RuntimeError as error:
        if not re.search(r'\bHTTP 404\b', str(error)):
            raise
        # GitHub community profiles can synthesize a nonexistent local contents URL
        # for an organization-wide default. Only trust the same owner's .github repo.
        shared = urlsplit(guide.get('html_url', ''))
        parts = shared.path.strip('/').split('/')
        if (shared.scheme != 'https' or shared.netloc != 'github.com' or len(parts) < 5
                or parts[0].lower() != repo.split('/')[0].lower() or parts[1:3] != ['.github', 'blob']):
            raise
        endpoint = f"repos/{parts[0]}/.github/contents/{quote('/'.join(parts[4:]), safe='/')}?ref={quote(parts[3], safe='')}"
        return command(['gh', 'api', endpoint, '-H', 'Accept: application/vnd.github.raw+json']), endpoint


def maintenance_notice(repo, text):
    # Recognize explicit notices about this repository, not generic dependency warnings.
    plain = re.sub(r'\[([^]]+)\]\([^)]*\)', r'\1', text)
    plain = re.sub(r'[*`>]', '', plain)
    plain = re.sub(r'\s+', ' ', plain)
    subject = rf'(?:this (?:project|repo(?:sitory)?)|(?:the )?{re.escape(repo)}(?: repository)?|{re.escape(repo.split("/")[1])})'
    # Archiving releases on Zenodo for citation does not retire the repository.
    archived = r'archived(?!\s+on\s+Zenodo\b)'
    patterns = [
        subject + rf'\s+(?:is|has been)\s+(?:no longer (?:(?:actively|being) )?maintained|unmaintained|deprecated|{archived}|read.only|in maintenance.only mode)',
        subject + r'\s+(?:has (?:moved|migrated)|is (?:moving|migrating))\s+to\b',
        subject + r'\s+has been superseded by\b',
        r'which has superseded\s+' + re.escape(repo.split('/')[1]) + r'\b',
        r'\b(?:we|this project|this repository)\s+(?:do not|no longer)\s+accept\s+(?:external\s+|new\s+)?(?:contributions|pull requests)\b',
    ]
    for pattern in patterns:
        match = re.search(pattern, plain, flags=re.I)
        if match:
            return 'Official maintenance/migration notice: ' + match.group(0)
    return None


def inspect_repository(repo, max_inactive_days, gh, command):
    meta = gh('api', f'repos/{repo}')
    if not isinstance(meta, dict) or not isinstance(meta.get('full_name'), str):
        raise ValueError('Invalid repository metadata')
    for flag in ('archived', 'disabled', 'has_issues'):
        if type(meta.get(flag)) is not bool:
            raise ValueError('Invalid repository flag: ' + flag)
    reason = None
    if meta['full_name'].lower() != repo.lower():
        reason = 'Repository moved to ' + meta['full_name'] + '; explicit allowlist update required'
    elif meta['archived'] or meta['disabled'] or not meta['has_issues']:
        reason = 'Repository is archived, disabled or has issues disabled'
    if not isinstance(meta.get('pushed_at'), str):
        raise ValueError('Invalid repository pushed_at')
    pushed = dt.datetime.fromisoformat(meta['pushed_at'].replace('Z', '+00:00'))
    if pushed.tzinfo is None:
        raise ValueError('Repository pushed_at must include a timezone')
    if (dt.datetime.now(dt.timezone.utc) - pushed).days > max_inactive_days:
        reason = reason or f'No repository push within {max_inactive_days} days; maintenance review required'
    if reason:
        return meta, None, None, None, reason
    readme = command(['gh', 'api', f'repos/{repo}/readme', '-H', 'Accept: application/vnd.github.raw+json'])
    rules, rules_url = contributing_guide(repo, gh, command)
    reason = maintenance_notice(repo, readme) or maintenance_notice(repo, rules or '')
    return meta, readme, rules, rules_url, reason


def project_items(repo, gh, number=None):
    """Query exact project URLs; a label or an unrelated project's Ready is insufficient."""
    owner, name = repo.split('/')
    fields = 'projectItems(first:20) { nodes { project { url } fieldValueByName(name:"Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } } } pageInfo { hasNextPage } }'
    selection = ('issue(number:$number) { state ' + fields + ' }' if number is not None else
                 'issues(first:1,states:OPEN) { nodes { ' + fields + ' } }')
    query = 'query($owner:String!,$name:String!' + (',$number:Int!' if number is not None else '') + ') { repository(owner:$owner,name:$name) { ' + selection + ' } }'
    args = ['api', 'graphql', '-f', 'query=' + query, '-f', 'owner=' + owner, '-f', 'name=' + name]
    if number is not None:
        args += ['-F', 'number=' + str(number)]
    response = gh(*args)
    if not isinstance(response, dict) or response.get('errors'):
        raise ValueError('Project status query failed: ' + str(response.get('errors') if isinstance(response, dict) else response))
    repository = response['data']['repository']
    if not isinstance(repository, dict):
        raise ValueError('Missing repository in project response')
    return repository


def ready_issue(repo, number, project_url, gh):
    if not number:
        return False
    issue = project_items(repo, gh, number)['issue']
    if not issue or issue['state'] != 'OPEN':
        return False
    items = issue['projectItems']
    if items['pageInfo']['hasNextPage']:
        raise ValueError('Issue project memberships truncated; cannot establish Ready status')
    return any(item['project']['url'] == project_url and
               (item.get('fieldValueByName') or {}).get('name') == 'Ready' for item in items['nodes'])
