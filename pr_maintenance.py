"""Verified local adoption and merge preparation for already published PRs.

This module never publishes, rewrites a branch, or infers approval from adoption.
Every merge resolution returns to the ordinary baseline/patch/review gates.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import quote


def identity(pr):
    return {key: pr.get(key) for key in
            ('headRefOid', 'headRefName', 'baseRefOid', 'baseRefName',
             'targetBaseOid', 'mergeable', 'mergeStateStatus')}


def conflict(pr):
    return pr.get('mergeable') == 'CONFLICTING' or pr.get('mergeStateStatus') == 'DIRTY'


def _sha(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[0-9a-f]{40}', value))


def target_base(repo, ref, gh):
    """PR base.sha can be a stale snapshot; read the actual target branch ref."""
    from contribute import ContributionPaused
    if not isinstance(ref, str) or not ref:
        raise ContributionPaused('PR target branch name is unavailable')
    result = gh('api', f'repos/{repo}/git/ref/heads/{quote(ref, safe="/")}')
    obj = result.get('object') or {}
    if result.get('ref') != 'refs/heads/' + ref or obj.get('type') != 'commit' or not _sha(obj.get('sha')):
        raise ContributionPaused('Current target branch tip could not be verified')
    return obj['sha']


def with_target_base(repo, pr, gh):
    # Legacy observations may not have a branch identity. They cannot start a
    # merge; preserve unknown instead of promoting the PR's snapshot SHA.
    if not pr.get('baseRefName'):
        return {**pr, 'targetBaseOid': None}
    return {**pr, 'targetBaseOid': target_base(repo, pr['baseRefName'], gh)}


def _verified_pr(repo, number, expected_head, expected_base, gh, *,
                 expected_target_base=None, allow_base_refresh=False):
    from contribute import ContributionPaused, repo_name
    from delivery import verified_fork
    repo_name(repo)
    if type(number) is not int or number < 1 or not _sha(expected_head) or not _sha(expected_base):
        raise ValueError('PR registration requires a number and exact head/base SHA values')
    login = gh('api', 'user')['login']
    pr = gh('api', f'repos/{repo}/pulls/{number}')
    head, base = pr.get('head') or {}, pr.get('base') or {}
    if (pr.get('number') != number or pr.get('state') != 'open'
            or pr.get('html_url', '').casefold() != f'https://github.com/{repo}/pull/{number}'.casefold()
            or (pr.get('user') or {}).get('login', '').casefold() != login.casefold()
            or (base.get('repo') or {}).get('full_name', '').casefold() != repo.casefold()
            or (head.get('repo') or {}).get('owner', {}).get('login', '').casefold() != login.casefold()
            or head.get('sha') != expected_head or not _sha(base.get('sha'))
            or not allow_base_refresh and base.get('sha') != expected_base
            or not isinstance(head.get('ref'), str) or not head['ref']
            or not isinstance(base.get('ref'), str) or not base['ref']):
        raise ContributionPaused('PR owner, target, open state or current head/base differs from the expected identity')
    fork = verified_fork(repo, login, (head.get('repo') or {}).get('full_name'), gh)
    pr['target_base_sha'] = target_base(repo, base['ref'], gh)
    if expected_target_base is not None and pr['target_base_sha'] != expected_target_base:
        raise ContributionPaused('Current target branch changed during PR registration; refresh the preview')
    return pr, fork, login


def _merge_head(work, command):
    path = Path(command(['git', 'rev-parse', '--git-path', 'MERGE_HEAD'], cwd=work).strip())
    path = path if path.is_absolute() else Path(work) / path
    return path.read_text(encoding='ascii').strip() if path.exists() else None


def _clean_head(work, expected, command):
    from contribute import ContributionPaused
    if command(['git', 'rev-parse', 'HEAD'], cwd=work).strip() != expected:
        raise ContributionPaused('Local HEAD differs from the published PR head; preserve it and reconcile manually')
    # status/diff intentionally trust these flags. Assume-unchanged is never
    # accepted. A configured sparse checkout may omit skip-worktree paths, but
    # an existing path under that flag can hide edits and is not safe to adopt.
    # Disable sparse handling for this read only: Git otherwise may clear a
    # skip-worktree flag as soon as it notices a path unexpectedly exists.
    entries = command(['git', '-c', 'core.sparseCheckout=false', 'ls-files', '-v', '-z'], cwd=work).split('\0')
    if any(entry and entry[0].islower() for entry in entries):
        raise ContributionPaused('Workspace contains skip-worktree or assume-unchanged entries; hidden local changes cannot be verified')
    sparse = [entry[2:] for entry in entries if entry.startswith('S ')]
    if sparse:
        configured = command(['git', 'config', '--bool', '--default', 'false', '--get', 'core.sparseCheckout'], cwd=work).strip() == 'true'
        if not configured or any(os.path.lexists(Path(work) / name) for name in sparse):
            raise ContributionPaused('Workspace contains skip-worktree entries with possible hidden local changes; only absent paths in configured sparse checkouts are allowed')
    status = command(['git', '-c', 'core.fsmonitor=false', 'status', '--porcelain',
                      '--untracked-files=all', '--ignore-submodules=none'], cwd=work)
    # These compare index/HEAD identities and tracked worktree changes without
    # synthesizing a tree. write-tree checks every blob and can hydrate an entire
    # partial clone even when the published checkout is already clean.
    staged = command(['git', 'diff-index', '--cached', '--name-only', '-z', 'HEAD', '--'], cwd=work)
    unstaged = command(['git', '-c', 'core.fsmonitor=false', 'diff-files', '--name-only', '-z',
                        '--ignore-submodules=none', '--'], cwd=work)
    if status.strip() or staged.strip() or unstaged.strip() or _merge_head(work, command):
        raise ContributionPaused('PR maintenance requires a clean workspace; existing local changes were preserved')


def register_existing_pr(pipeline, repo, number, workspace, baseline_sha, *,
                         expected_head, expected_base, task_id=None, issue_number=None,
                         plan=None, apply=False, expected_target_base=None):
    """Preview or atomically bind an owned open PR to a verified local checkout.

``baseline_sha`` is the original regression baseline, which can precede the
current PR base. A supplied plan must contain a valid executable ``spec``;
without one the task observes and reports pending maintenance, but cannot edit.
Omitting task_id creates a separate maintenance task, leaving old failures and
their workspaces intact. Existing nonterminal tasks can be explicitly adopted.
``expected_base`` pins the PR metadata snapshot for compatibility; the receipt
separately pins the live target ref as ``target_base_sha``. Optionally supply
``expected_target_base`` to pin that ref before preview as well.
"""
    from autonomous import validate_spec
    from contribute import ContributionPaused, command, gh, now
    from scheduling import TERMINAL
    if not _sha(baseline_sha):
        raise ValueError('A verified full regression baseline SHA is required')
    if issue_number is not None and (type(issue_number) is not int or issue_number < 1):
        raise ValueError('Associated issue number must be a positive integer')
    work = Path(workspace).resolve(strict=True)
    pr, fork, login = _verified_pr(repo, number, expected_head, expected_base, gh,
                                   expected_target_base=expected_target_base)
    _clean_head(work, expected_head, command)
    command(['git', 'merge-base', '--is-ancestor', baseline_sha, expected_head], cwd=work)
    tree = command(['git', 'rev-parse', 'HEAD^{tree}'], cwd=work).strip()
    # Require a repository remote matching the verified target or fork as well
    # as content identity; a coincidentally matching unrelated checkout is not adopted.
    remotes = command(['git', 'remote', '-v'], cwd=work).splitlines()
    names = {repo.casefold(), fork['full_name'].casefold()}
    def remote_name(line):
        fields = line.split()
        if len(fields) < 2:
            return None
        match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([^\s]+?)(?:\.git)?', fields[1])
        return match.group(1).casefold() if match else None
    if not any(remote_name(line) in names for line in remotes):
        raise ContributionPaused('Workspace has no remote matching the verified PR target or fork')
    key = task_id or repo.replace('/', '--') + '--pr-' + str(number)
    existing = pipeline.db.execute('SELECT * FROM tasks WHERE id=?', (key,)).fetchone()
    tables = {r[0] for r in pipeline.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    old_data = None
    if 'automation' in tables:
        row = pipeline.db.execute('SELECT data FROM automation WHERE task=?', (key,)).fetchone()
        old_data = json.loads(row[0]) if row else None
        for row in pipeline.db.execute('SELECT t.id,t.repo,a.data FROM tasks t JOIN automation a ON a.task=t.id'):
            other = json.loads(row['data'])
            if row['repo'].casefold() == repo.casefold() and other.get('pr_number') == number and row['id'] != key:
                raise ContributionPaused('PR is already linked to task ' + row['id'] + '; preserve its maintenance history')
    if existing:
        if (existing['repo'].casefold() != repo.casefold() or existing['state'] in TERMINAL
                or existing['workspace'] and Path(existing['workspace']).resolve() != work
                or old_data and old_data.get('pr_number') not in (None, number)
                or old_data and old_data.get('pr_number') == number and old_data.get('stage') != 'PR_OPEN'):
            raise ContributionPaused('Existing task contains different or terminal work; create a separate maintenance task')
        if issue_number is not None and existing['number'] not in (None, issue_number):
            raise ContributionPaused('Associated issue differs from the existing task')
    adopted_plan = copy.deepcopy(plan)
    if adopted_plan is not None:
        if not isinstance(adopted_plan, dict):
            raise ValueError('Maintenance plan must be an object')
        validate_spec(adopted_plan.get('spec'))
        adopted_plan.update(pr_title=pr['title'], pr_body=pr.get('body') or '')
    receipt = {'repo': repo, 'number': number, 'author': login, 'head_repo': fork['full_name'],
               'head_sha': expected_head, 'head_ref': pr['head']['ref'],
               'base_sha': expected_base, 'base_ref': pr['base']['ref'],
               'target_base_sha': pr['target_base_sha'],
               'workspace': str(work), 'regression_baseline': baseline_sha,
               'content_fingerprint': 'tree-v2:' + tree, 'observed_at': now(),
               'verification_reused': False, 'observation_only': adopted_plan is None}
    result = {'task_id': key, 'applied': False, 'receipt': receipt}
    if not apply:
        return result
    # Re-read immediately before the local write; never bind stale remote identity.
    _verified_pr(repo, number, expected_head, expected_base, gh,
                 expected_target_base=pr['target_base_sha'])
    _clean_head(work, expected_head, command)
    old_receipt = (old_data or {}).get('pr_registration') or {}
    same_receipt = all(old_receipt.get(name) == value for name, value in receipt.items() if name != 'observed_at')
    if same_receipt and (old_data or {}).get('plan') == adopted_plan:
        return {'task_id': key, 'applied': True, 'already_registered': True, 'receipt': old_receipt}
    data = copy.deepcopy(old_data or {'attempts': {}})
    if existing:
        data.setdefault('pr_registration_history', []).append(
            {'at': now(), 'task': dict(existing), 'automation': old_data})
    # Adoption does not authorize reuse of another patch's receipts or publication intent.
    for name in ('approved_fingerprint', 'expected_tree', 'publish_commit', 'push_intent',
                 'push_verified', 'feedback_digest', 'pending_body_digest', 'pr_merge'):
        data.pop(name, None)
    data.update(stage='PR_OPEN', pr_number=number, pr_url=pr['html_url'], branch=pr['head']['ref'],
                pr_create_pending=False, maintenance_observation_only=adopted_plan is None,
                pr_registration=receipt,
                publication_fork={'full_name': fork['full_name'], 'clone_url': fork['clone_url'],
                                  'parent': repo, 'owner': login, 'verified_at': now()})
    if adopted_plan is not None:
        data['plan'] = adopted_plan
    else:
        data.pop('plan', None)
    with pipeline.db:
        pipeline.db.execute('CREATE TABLE IF NOT EXISTS automation(task TEXT PRIMARY KEY,data TEXT NOT NULL)')
        if not existing:
            pipeline.db.execute('''INSERT INTO tasks
                (id,repo,number,source,title,url,score,state,holds,payload,updated,workspace,base_sha)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (key, repo, issue_number, 'pr_maintenance', pr['title'], pr['html_url'], 100,
                 'PR_OPEN', '[]', json.dumps({'pr_registration': receipt}), now(), str(work), baseline_sha))
        else:
            pipeline.db.execute('UPDATE tasks SET state=?,workspace=?,base_sha=?,number=COALESCE(number,?),updated=? WHERE id=?',
                                ('PR_OPEN', str(work), baseline_sha, issue_number, now(), key))
        pipeline.db.execute('INSERT INTO automation VALUES (?,?) ON CONFLICT(task) DO UPDATE SET data=excluded.data',
                            (key, json.dumps(data)))
        pipeline.db.execute('INSERT INTO events(task,event,at) VALUES (?,?,?)',
                            (key, 'Verified existing PR registration: ' + pr['html_url'], now()))
    return {**result, 'applied': True}


def queue_conflict(engine, task_id, data, pr):
    """Record an actionable exact-head/base conflict once, without calling a model."""
    from contribute import now
    item = identity(pr)
    def merge_key(value):
        return hashlib.sha256(json.dumps({name: value.get(name) for name in
            ('headRefOid', 'headRefName', 'baseRefName', 'targetBaseOid')}, sort_keys=True).encode()).hexdigest()
    key = merge_key(item)
    prior = data.get('pr_merge') or {}
    if prior and merge_key(prior.get('identity') or {}) == key:
        if prior.get('identity') != item:
            prior.setdefault('metadata_history', []).append({'at': now(), 'identity': prior['identity']})
            prior.update(identity=item, key=key)
            engine.store(task_id, data)
        return prior.get('state') == 'pending'
    if prior:
        data.setdefault('pr_merge_history', []).append(copy.deepcopy(prior))
    from autonomous import validate_spec
    valid = _sha(item['headRefOid']) and _sha(item['baseRefOid']) and _sha(item['targetBaseOid'])
    try:
        validate_spec((data.get('plan') or {}).get('spec'))
        recipe = not data.get('maintenance_observation_only')
    except ValueError:
        recipe = False
    state = 'pending' if valid and recipe else 'needs_verification_recipe' if valid else 'identity_unavailable'
    data['pr_merge'] = {'key': key, 'identity': item, 'state': state, 'observed_at': now(),
                        'old_baseline': engine.p.task(task_id)['base_sha']}
    data['feedback'] = {'merge_conflict': item,
                        'requirement': 'Merge the exact upstream base, resolve only published-scope conflicts, then rerun baseline/patch checks and review.'}
    engine.store(task_id, data)
    engine.p.event(task_id, 'PR merge conflict queued: ' + state)
    return state == 'pending'


def prepare_merge(engine, task_id, data):
    """Start or resume one ordinary local merge of the observed upstream base."""
    from contribute import ContributionPaused, command, execute, gh, now
    item = data.get('pr_merge') or {}
    if item.get('state') == 'target_changed':
        raise ContributionPaused('Target branch changed during a local merge; preserve and reconcile the saved workspace before continuing')
    if item.get('state') not in ('pending', 'merging', 'resolved_unverified'):
        return
    task = engine.p.task(task_id)
    work, observed = task['workspace'], item['identity']
    head, base = observed['headRefOid'], observed.get('targetBaseOid')
    pr, _, _ = _verified_pr(task['repo'], data['pr_number'], head, observed['baseRefOid'], gh,
                           allow_base_refresh=True)
    if pr['head']['ref'] != data.get('branch'):
        raise ContributionPaused('Published branch differs from the registered maintenance branch')
    merge_head = _merge_head(work, command)
    if pr['base']['ref'] != observed['baseRefName'] or pr['target_base_sha'] != base:
        latest = {**observed, 'baseRefName': pr['base']['ref'], 'baseRefOid': pr['base']['sha'],
                  'targetBaseOid': pr['target_base_sha']}
        if merge_head or pr['base']['ref'] != observed['baseRefName']:
            item.update(state='target_changed', latest_identity=latest, changed_at=now())
            engine.store(task_id, data)
            raise ContributionPaused('Target branch changed during maintenance; local merge was preserved for reconciliation')
        _clean_head(work, head, command)
        queue_conflict(engine, task_id, data, latest)
        raise ContributionPaused('Target branch changed; refreshed exact maintenance identity before editing; retry from the saved queue')
    if pr['base']['sha'] != observed['baseRefOid']:
        item.setdefault('metadata_history', []).append({'at': now(), 'identity': copy.deepcopy(observed)})
        observed['baseRefOid'] = pr['base']['sha']
        engine.store(task_id, data)
    if merge_head:
        if merge_head != base or command(['git', 'rev-parse', 'HEAD'], cwd=work).strip() != head:
            raise ContributionPaused('Existing local merge differs from the saved maintenance intent')
    else:
        _clean_head(work, head, command)
        command(['git', 'fetch', '--no-tags', 'https://github.com/' + task['repo'] + '.git', base], cwd=work)
        if command(['git', 'rev-parse', base + '^{commit}'], cwd=work).strip() != base:
            raise ContributionPaused('Fetched upstream base identity was not confirmed')
        if target_base(task['repo'], observed['baseRefName'], gh) != base:
            raise ContributionPaused('Target branch changed during fetch; retry observation before merging')
        # Persist intent before Git changes the index; interrupted merges resume above.
        item.update(state='merging', started_at=now())
        data.pop('approved_fingerprint', None)
        engine.store(task_id, data)
        result = execute(['git', 'merge', '--no-commit', '--no-ff', '--no-edit', base], cwd=work)
        if _merge_head(work, command) != base:
            raise ContributionPaused('Local merge did not establish the expected base; inspect saved workspace: ' + (result.stderr or result.stdout)[-1000:])
        if result.returncode not in (0, 1):
            raise RuntimeError('Local merge failed: ' + (result.stderr or result.stdout)[-1000:])
    item['state'] = 'merging'
    item['conflicted_paths'] = [x for x in command(['git', 'diff', '--name-only', '--diff-filter=U', '-z'], cwd=work).split('\0') if x]
    with engine.db:
        engine.db.execute('UPDATE tasks SET base_sha=?,updated=? WHERE id=?', (base, now(), task_id))
    engine.store(task_id, data)


def finish_merge_resolution(engine, task_id, data):
    from contribute import ContributionPaused, command, execute, now
    item = data.get('pr_merge') or {}
    if item.get('state') != 'merging':
        return
    work = engine.p.task(task_id)['workspace']
    observed = item['identity']
    if (_merge_head(work, command) != observed['targetBaseOid']
            or command(['git', 'rev-parse', 'HEAD'], cwd=work).strip() != observed['headRefOid']):
        raise ContributionPaused('Local merge identity changed during resolution')
    # diff --check detects conflict markers without attempting to resolve them.
    checked = execute(['git', 'diff', '--check'], cwd=work)
    if checked.returncode:
        raise RuntimeError('Merge resolution still contains conflict markers or whitespace errors: ' + (checked.stdout + checked.stderr)[-2000:])
    paths = [x for x in command(['git', 'diff', '--name-only', '--diff-filter=U', '-z'], cwd=work).split('\0') if x]
    if paths:
        command(['git', 'add', '--', *paths], cwd=work)
    if command(['git', 'ls-files', '-u'], cwd=work).strip():
        raise RuntimeError('Merge still contains unresolved index entries')
    item.update(state='resolved_unverified', resolved_at=now())
    data.pop('approved_fingerprint', None)
    engine.store(task_id, data)


def validate_pending_merge(task, data, command):
    """The verified uncommitted merge must still name both exact original parents."""
    from contribute import ContributionPaused
    item = data['pr_merge']['identity']
    work = task['workspace']
    merge_head = _merge_head(work, command)
    head = command(['git', 'rev-parse', 'HEAD'], cwd=work).strip()
    pending = merge_head == item['targetBaseOid'] and head == item['headRefOid']
    # A hook/transport failure can lose the response after Git made the commit.
    # Recover only the exact approved tree and two expected parents.
    recovered = (merge_head is None and data.get('expected_tree')
                 and command(['git', 'rev-parse', 'HEAD^{tree}'], cwd=work).strip() == data['expected_tree']
                 and command(['git', 'show', '-s', '--format=%P', 'HEAD'], cwd=work).strip().split()
                 == [item['headRefOid'], item['targetBaseOid']])
    if (task['base_sha'] != item['targetBaseOid'] or not (pending or recovered)
            or command(['git', 'ls-files', '-u'], cwd=work).strip()):
        raise ContributionPaused('Verified merge parents or resolution changed before publication')


def validate_publication_target(engine, task_id, data):
    """Recheck the owned PR and actual target ref immediately before commit/push."""
    from contribute import ContributionPaused, gh, now
    item = data.get('pr_merge') or {}
    if not item:
        return
    observed = item['identity']
    task = engine.p.task(task_id)
    latest = gh('api', f"repos/{task['repo']}/pulls/{data['pr_number']}")
    remote_head = (latest.get('head') or {}).get('sha')
    published = data.get('push_verified')
    if not published:
        # A subsequent review archives the previous publication's intent. Its
        # verified ref/commit remains the exact expected remote head, including
        # later ordinary revisions after the original merge publication.
        published = next((record.get('previous', {}).get('push_verified')
                          for record in reversed(data.get('publication_history', []))
                          if record.get('previous', {}).get('push_verified')), None)
    if published:
        if published.get('ref') != 'refs/heads/' + data.get('branch', '') or not _sha(published.get('commit')):
            raise ContributionPaused('Saved publication head does not match the maintenance branch')
        allowed = {published['commit']}
    else:
        allowed = {observed['headRefOid']}
    # A lost push response can already have published the saved exact commit.
    if _sha(data.get('publish_commit')):
        allowed.add(data['publish_commit'])
    latest_identity = None
    try:
        if remote_head not in allowed:
            raise ContributionPaused('Published PR head changed during maintenance')
        pr, _, _ = _verified_pr(task['repo'], data['pr_number'], remote_head,
                               observed['baseRefOid'], gh, allow_base_refresh=True)
        latest_identity = {'headRefOid': remote_head, 'headRefName': pr['head']['ref'],
                           'baseRefOid': pr['base']['sha'], 'baseRefName': pr['base']['ref'],
                           'targetBaseOid': pr['target_base_sha']}
        if (pr['head']['ref'] != data.get('branch') or pr['base']['ref'] != observed['baseRefName']
                or pr['target_base_sha'] != observed['targetBaseOid']):
            raise ContributionPaused('Current target branch changed before publication; retain the local merge and revalidate against the new target')
    except ContributionPaused as error:
        data['maintenance_pending'] = {'reason': 'publication_identity_changed',
                                       'detail': str(error), 'at': now(),
                                       'expected_identity': copy.deepcopy(observed),
                                       'observed_identity': latest_identity,
                                       'remote_head': remote_head}
        engine.store(task_id, data)
        raise
    if (data.get('maintenance_pending') or {}).get('reason') == 'publication_identity_changed':
        data.pop('maintenance_pending')
        engine.store(task_id, data)
