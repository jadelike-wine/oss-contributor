"""Small scheduling primitives; no model calls, network access or publication.

Task aliases preserve every historical task. A canonical identity is a verified
repository name plus issue number, never a title or a guessed semantic match.
"""
import datetime as dt
import hashlib
import json
import re
from urllib.parse import urlsplit


MAINTENANCE = {'PR_OPEN', 'WAITING_MAINTAINER', 'ISSUE', 'PUBLISH'}
IMPLEMENTATION = {'REPRODUCE', 'REPAIR', 'PATCH_READY', 'AUTO_REVIEW'}
TERMINAL = {'HOLD', 'SKIPPED', 'CLOSED', 'MERGED', 'FAILED'}


def canonical_repo(repo, canonical_names=None):
    """Normalize case; rename aliases must come from verified repository metadata."""
    value = repo.strip().rstrip('/')
    if value.startswith(('https://github.com/', 'http://github.com/')):
        value = urlsplit(value).path.strip('/')
    if value.endswith('.git'):
        value = value[:-4]
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        raise ValueError('Expected owner/repository identity')
    value = value.casefold()
    aliases = {key.casefold(): target.casefold() for key, target in (canonical_names or {}).items()}
    visited = set()
    while value in aliases and aliases[value] != value:
        if value in visited:
            raise ValueError('Repository canonical-name cycle')
        visited.add(value)
        value = aliases[value]
    return value


def issue_identity(repo, number, canonical_names=None):
    if type(number) is not int or number <= 0:
        raise ValueError('Issue number must be a positive integer')
    return canonical_repo(repo, canonical_names) + '#' + str(number)


def task_lane(task, data, queue=None):
    stage = data.get('stage', task.get('state', 'PLAN'))
    if stage in MAINTENANCE:
        return 'maintenance'
    if stage in IMPLEMENTATION or task.get('number') or queue == 'READY_TO_IMPLEMENT':
        return 'implementation'
    # An approved proposal with no confirmed direction must not consume a code slot.
    if queue == 'NEEDS_MAINTAINER_CONFIRMATION':
        return 'maintenance'
    return 'research'


def priority_key(task, data, queue=None):
    """Fair rotation is applied within these lanes by the caller."""
    stage = data.get('stage', task.get('state', 'PLAN'))
    if data.get('pr_number') and stage in IMPLEMENTATION:
        return 0  # Actual feedback already moved a published PR to a code stage.
    if stage == 'PR_OPEN':
        return 1
    if stage in {'PUBLISH', 'AUTO_REVIEW', 'PATCH_READY'}:
        return 2
    if stage in {'REPAIR', 'REPRODUCE'}:
        return 3
    if queue == 'READY_TO_IMPLEMENT':
        return 4
    if task.get('number') and stage in {'PLAN', 'DISCOVERED'}:
        return 5
    if stage in MAINTENANCE or queue == 'NEEDS_MAINTAINER_CONFIRMATION':
        return 6
    return 7


class RunTaskBudget:
    """Bound unique task dispatches, separately from model-call/time budgets.

    Reserve before dispatch so crashes and no-op attempts remain bounded. A task
    may pass through multiple stages in its lane without consuming another slot.
    A failed attempt consumes only its own slot, never the entire implementation
    allowance. Existing PR follow-up that enters REPAIR must reserve a code slot.
    """
    def __init__(self, config):
        options = config.get('automation', {})
        fallback = options.get('max_tasks_per_tick', 1)
        self.limits = {
            'implementation': options.get('implementation_tasks_per_tick', fallback),
            'maintenance': options.get('maintenance_tasks_per_tick', fallback),
            'research': config.get('research', {}).get('candidates_per_run', fallback),
        }
        for lane, limit in self.limits.items():
            if type(limit) is not int or not 0 <= limit <= 100:
                raise ValueError(lane + ' task budget must be an integer from 0 to 100')
        self.used = {lane: set() for lane in self.limits}

    def admit(self, task_id, lane):
        seen = self.used[lane]
        if task_id in seen:
            return True
        if len(seen) >= self.limits[lane]:
            return False
        seen.add(task_id)
        return True


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS task_aliases (
        task TEXT PRIMARY KEY, canonical_task TEXT NOT NULL,
        identity TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS task_alias_identity ON task_aliases(identity)')


def _automation(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation'").fetchone():
        return {}
    return {row[0]: json.loads(row[1]) for row in db.execute('SELECT task,data FROM automation')}


def _canonical_task(rows, data):
    # Preserve the most advanced work. Tie-break by stable ID, not scan order.
    def key(row):
        item = dict(row)
        saved = data.get(item['id'], {})
        published = bool(saved.get('pr_number')) or item['state'] in {'PR_OPEN', 'MERGED'}
        terminal = item['state'] in TERMINAL
        return (not published, terminal, priority_key(item, saved), item['id'])
    return min(rows, key=key)['id']


def reconcile_issue_duplicates(db, canonical_names=None, at=None):
    """Add references only; retain task rows, events, worktrees and publication IDs.

    Scheduling should skip an aliased task only when it has no published PR.
    Distinct already-published PRs still require their own maintenance.
    """
    migrate(db)
    at = at or dt.datetime.now(dt.timezone.utc).isoformat()
    groups = {}
    for row in db.execute('SELECT * FROM tasks WHERE number IS NOT NULL'):
        groups.setdefault(issue_identity(row['repo'], row['number'], canonical_names), []).append(row)
    data = _automation(db)
    linked = []
    with db:
        for identity, rows in groups.items():
            if len(rows) < 2:
                continue
            existing = db.execute('SELECT canonical_task FROM task_aliases WHERE identity=? ORDER BY task',
                                  (identity,)).fetchall()
            candidates = {row['id'] for row in rows}
            stable = {row[0] for row in existing if row[0] in candidates}
            canonical = next(iter(stable)) if len(stable) == 1 else _canonical_task(rows, data)
            for row in rows:
                if row['id'] == canonical:
                    continue
                db.execute('INSERT INTO task_aliases VALUES (?,?,?,?,?) ON CONFLICT(task) DO UPDATE SET '
                           'canonical_task=excluded.canonical_task,identity=excluded.identity,reason=excluded.reason',
                           (row['id'], canonical, identity, 'same canonical repository and issue number', at))
                linked.append((row['id'], canonical))
    return linked


def find_issue_task(db, repo, number, canonical_names=None):
    identity = issue_identity(repo, number, canonical_names)
    rows = [row for row in db.execute('SELECT * FROM tasks WHERE number=?', (number,))
            if issue_identity(row['repo'], row['number'], canonical_names) == identity]
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]['id']
    return _canonical_task(rows, _automation(db))


def stable_focus(focus):
    """A calendar boundary is not new source evidence."""
    return re.sub(r'\s+Research batch \d{4}-W\d{2}\s*$', '', focus).strip()


def existing_hunt(db, repo, focus, canonical_names=None):
    normalized, target = stable_focus(focus), canonical_repo(repo, canonical_names)
    rows = [row for row in db.execute("SELECT * FROM tasks WHERE source='hunt' ORDER BY updated,id")
            if canonical_repo(row['repo'], canonical_names) == target
            and stable_focus(json.loads(row['payload']).get('focus', '')) == normalized]
    return _canonical_task(rows, _automation(db)) if rows else None


RESEARCH_CHANGE_FIELDS = ('scoped_source_digest', 'policy_digest', 'demand_digest', 'competition_digest')


def research_reassessment(previous, current):
    """Return explicit material-evidence changes; time or a new batch is insufficient.

    Digests must be controller-collected over the relevant inspected scope. A new
    default-branch SHA alone is not enough. Missing/failed collection never proves
    changed evidence or absence of a capability.
    """
    if current.get('complete') is not True:
        return []
    return [key for key in RESEARCH_CHANGE_FIELDS
            if previous.get(key) and current.get(key) and previous[key] != current[key]]


def scan_key(repo, scan_limit):
    return hashlib.sha256(json.dumps([canonical_repo(repo), scan_limit, 200, 1]).encode()).hexdigest()


def reusable_scan_record(previous, repo, now, ttl_seconds, scan_limit):
    """Reuse only complete success; never refresh its observation timestamp."""
    if ttl_seconds <= 0:
        return None
    key = scan_key(repo, scan_limit)
    for record in previous.get('repositories', []):
        if record.get('repo', '').casefold() != repo.casefold() or record.get('scan_key') != key:
            continue
        if record.get('status') not in {'scanned', 'cached', 'skipped'} or record.get('error'):
            continue
        try:
            observed = dt.datetime.fromisoformat(record['observed_at'])
            current = dt.datetime.fromisoformat(now)
            age = (current - observed).total_seconds()
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= age < ttl_seconds:
            continue
        return {**record, 'repo': repo, 'status': 'cached', 'changed': 0,
                'cached_status': record.get('cached_status', record['status'])}
    return None
