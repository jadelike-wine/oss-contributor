"""Read-only repository health checks and upstream contribution gates (stdlib only)."""
import datetime as dt
import re


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
    return command(['gh', 'api', url, '-H', 'Accept: application/vnd.github.raw+json']), url


def maintenance_notice(repo, text):
    # Recognize explicit notices about this repository, not generic dependency warnings.
    plain = re.sub(r'\[([^]]+)\]\([^)]*\)', r'\1', text)
    plain = re.sub(r'[*`>]', '', plain)
    plain = re.sub(r'\s+', ' ', plain)
    subject = rf'(?:this (?:project|repo(?:sitory)?)|(?:the )?{re.escape(repo)}(?: repository)?|{re.escape(repo.split("/")[1])})'
    patterns = [
        subject + r'\s+(?:is|has been)\s+(?:no longer (?:(?:actively|being) )?maintained|unmaintained|deprecated|archived|read.only|in maintenance.only mode)',
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
