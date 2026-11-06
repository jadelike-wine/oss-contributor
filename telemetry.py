"""Additive SQLite observations and enforceable call/time budgets (stdlib only).

No prompts, command arguments, output or exception messages are stored here.
NULL usage is unknown, never a zero-cost claim. Contexts nest within one run.
"""
import contextlib
import contextvars
import datetime as dt
import functools
import json
import math
import os
import sqlite3
import subprocess
import time
import uuid

ACTIVE = contextvars.ContextVar('oss_run', default=None)


class BudgetExhausted(RuntimeError):
    pass


class DeliveryBudgetExhausted(BudgetExhausted):
    """Yield the remaining reserved budget to research, without raising total caps."""


class TaskBudgetExhausted(BudgetExhausted):
    """Pause one task while other tasks may use the remaining run budget."""


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def migrate(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS research_candidates (
      feature_id TEXT PRIMARY KEY, task TEXT NOT NULL, repo TEXT NOT NULL,
      category TEXT NOT NULL, queue TEXT NOT NULL, priority INTEGER NOT NULL,
      matrix TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS runs (
      run_id TEXT PRIMARY KEY, entry TEXT NOT NULL, started TEXT NOT NULL, ended TEXT,
      elapsed REAL NOT NULL DEFAULT 0, outcome TEXT NOT NULL, stop_reason TEXT,
      counters TEXT NOT NULL DEFAULT '{}');
    CREATE TABLE IF NOT EXISTS stages (
      stage_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task TEXT, name TEXT NOT NULL,
      started TEXT NOT NULL, ended TEXT, elapsed REAL, outcome TEXT NOT NULL, reason TEXT);
    CREATE TABLE IF NOT EXISTS model_calls (
      call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task TEXT, purpose TEXT,
      model TEXT, channel TEXT, started TEXT NOT NULL, ended TEXT, outcome TEXT,
      input_tokens INTEGER, output_tokens INTEGER, actual_cost_usd REAL);
    CREATE INDEX IF NOT EXISTS model_calls_day ON model_calls(started);
    CREATE INDEX IF NOT EXISTS stages_run ON stages(run_id);
    CREATE TABLE IF NOT EXISTS pr_observations (
      repo TEXT NOT NULL, number INTEGER NOT NULL, head_sha TEXT, state TEXT NOT NULL,
      ci_state TEXT NOT NULL, maintainer_response INTEGER NOT NULL, observed_at TEXT NOT NULL,
      PRIMARY KEY(repo,number));
    CREATE TABLE IF NOT EXISTS pr_acceptance_observations (
      repo TEXT NOT NULL, number INTEGER NOT NULL, head_sha TEXT,
      review_decision TEXT, human_approval INTEGER NOT NULL,
      merged_at TEXT, state TEXT NOT NULL, observed_at TEXT NOT NULL,
      PRIMARY KEY(repo,number));
    CREATE TABLE IF NOT EXISTS pr_maintenance_observations (
      repo TEXT NOT NULL, number INTEGER NOT NULL, head_sha TEXT, base_sha TEXT,
      base_ref TEXT, mergeable TEXT, merge_state TEXT, observed_at TEXT NOT NULL,
      PRIMARY KEY(repo,number));
    ''')
    if 'owner_pid' not in {r[1] for r in db.execute('PRAGMA table_info(runs)')}:
        db.execute('ALTER TABLE runs ADD COLUMN owner_pid INTEGER')
        db.commit()
    if 'target_base_sha' not in {r[1] for r in db.execute('PRAGMA table_info(pr_maintenance_observations)')}:
        db.execute('ALTER TABLE pr_maintenance_observations ADD COLUMN target_base_sha TEXT')
        db.commit()


def recover_interrupted(db, process_running, *, legacy_processes_checked=False):
    """Only close dead owned runs; legacy rows require an explicit process audit."""
    locks = db.execute('SELECT * FROM locks').fetchall()
    if any(process_running(r['pid']) or process_running(r['child_pid']) for r in locks):
        return []
    recovered = []
    for row in db.execute('SELECT * FROM runs WHERE ended IS NULL').fetchall():
        pid = row['owner_pid']
        if (pid and process_running(pid)) or (not pid and not legacy_processes_checked):
            continue
        recovered_at = utc()
        checkpoint = (dt.datetime.fromisoformat(row['started']) + dt.timedelta(seconds=max(0, row['elapsed'] or 0))).isoformat()
        reason = ('owner_process_stopped; resume_from_saved_task_stage; end_and_elapsed_are_last_checkpoint; '
                  'actual_stop_unknown; recovered_at=' + recovered_at)
        with db:
            db.execute("UPDATE runs SET ended=?,outcome='interrupted',stop_reason=? WHERE run_id=? AND ended IS NULL",
                       (checkpoint, reason, row['run_id']))
            db.execute("UPDATE stages SET ended=CASE WHEN started>? THEN started ELSE ? END,outcome='interrupted',reason=? WHERE run_id=? AND ended IS NULL",
                       (checkpoint, checkpoint, reason, row['run_id']))
            db.execute("UPDATE model_calls SET ended=CASE WHEN started>? THEN started ELSE ? END,outcome='interrupted' WHERE run_id=? AND ended IS NULL",
                       (checkpoint, checkpoint, row['run_id']))
        recovered.append(row['run_id'])
    return recovered


class Run:
    def __init__(self, pipeline, entry):
        self.p, self.db = pipeline, pipeline.db
        self.options = pipeline.config.get('budgets', {})
        for key, value in self.options.items():
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError('Budget must be a finite nonnegative number: ' + key)
        self.id, self.start = uuid.uuid4().hex, time.monotonic()
        self.started, self.counters, self.deadlines = utc(), {}, []
        self.reason = None
        self.outcome = 'success'
        self.implementation_tasks = set()
        from scheduling import RunTaskBudget
        self.task_budget = RunTaskBudget(pipeline.config)
        self.channel_failures = {}
        self.reserve_research = False
        self.call_id = None
        self.task = None
        self.task_elapsed = {}
        self.paused_tasks = set()
        self.task_active_since = self.start
        with self.db:
            self.db.execute('INSERT INTO runs(run_id,entry,started,outcome,owner_pid) VALUES (?,?,?,?,?)',
                            (self.id, entry, self.started, 'running', os.getpid()))

    def count(self, key, amount=1):
        self.counters[key] = self.counters.get(key, 0) + amount
        self.checkpoint()

    def checkpoint(self):
        with self.db:
            self.db.execute('UPDATE runs SET elapsed=?,counters=?,stop_reason=? WHERE run_id=?',
                            (time.monotonic() - self.start, json.dumps(self.counters), self.reason, self.id))

    def stop(self, reason):
        self.reason = reason
        self.checkpoint()
        raise BudgetExhausted(reason)

    def remaining(self):
        current = time.monotonic()
        daily = self.db.execute('SELECT coalesce(sum(elapsed),0) FROM runs WHERE started>=? AND run_id!=?',
                                (self.started[:10], self.id)).fetchone()[0]
        global_remaining = min(self.start + self.options.get('run_seconds', 5400) - current,
                               self.options.get('daily_seconds', 21600) - daily - (current - self.start))
        if global_remaining <= 0:
            self.stop('time_budget_exhausted')
        limits = [global_remaining]
        if self.task:
            spent = self.task_elapsed.get(self.task, 0) + current - self.task_active_since
            task_remaining = self.options.get('task_seconds', 3600) - spent
            if task_remaining <= 0:
                self.paused_tasks.add(self.task)
                self.count('task_budget_pauses')
                raise TaskBudgetExhausted('task_time_budget_exhausted')
            limits.append(task_remaining)
        stage_remaining = min(self.deadlines, default=float('inf')) - current
        if stage_remaining <= 0:
            if self.task:
                self.paused_tasks.add(self.task)
                self.count('task_budget_pauses')
                raise TaskBudgetExhausted('task_stage_time_budget_exhausted')
            self.stop('time_budget_exhausted')
        limits.append(stage_remaining)
        if self.reserve_research:
            reserve = self.p.config.get('research', {}).get('reserve_seconds', 600)
            if current - self.start >= max(0, self.options.get('run_seconds', 5400) - reserve):
                raise DeliveryBudgetExhausted('delivery_time_share_exhausted')
        return min(limits)

    def switch_task(self, task):
        if task == self.task:
            return
        current = time.monotonic()
        if self.task:
            self.task_elapsed[self.task] = self.task_elapsed.get(self.task, 0) + current - self.task_active_since
        self.task, self.task_active_since = task, current

    def reserve_call(self, task, purpose, model, channel):
        self.remaining()
        if self.reserve_research:
            used = self.db.execute('SELECT count(*) FROM model_calls WHERE run_id=?', (self.id,)).fetchone()[0]
            reserve = self.p.config.get('research', {}).get('reserve_model_calls', 2)
            if used >= max(0, self.options.get('run_model_calls', 16) - reserve):
                raise DeliveryBudgetExhausted('delivery_model_share_exhausted')
        checks = [('run_model_calls', 16, 'run_id=?', (self.id,)),
                  ('daily_model_calls', None, 'started>=?', (self.started[:10],)),
                  ('task_model_calls', 40, 'task=?', (task,))]
        # Reserve before launching, including failed/interrupted calls. A crash cannot refund it.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for key, default, where, params in checks:
                limit = self.options.get(key, default)
                if limit is None:
                    continue
                count = self.db.execute('SELECT count(*) FROM model_calls WHERE ' + where, params).fetchone()[0]
                if count >= limit:
                    self.db.rollback()
                    if key == 'task_model_calls':
                        self.paused_tasks.add(task)
                        self.count('task_budget_pauses')
                        raise TaskBudgetExhausted('task_model_calls_exhausted')
                    self.stop(key + '_exhausted')
            call_id = uuid.uuid4().hex
            self.db.execute('INSERT INTO model_calls(call_id,run_id,task,purpose,model,channel,started,outcome) '
                            'VALUES (?,?,?,?,?,?,?,?)', (call_id, self.id, task, purpose, model, channel, utc(), 'running'))
        self.count('model_calls')
        return call_id


@contextlib.contextmanager
def run(pipeline, entry):
    if ACTIVE.get() is not None:
        yield ACTIVE.get()
        return
    current = Run(pipeline, entry)
    token = ACTIVE.set(current)
    outcome = 'success'
    try:
        yield current
    except BaseException as error:
        outcome = classify(error)
        current.reason = current.reason or type(error).__name__
        raise
    finally:
        try:
            current.checkpoint()
            with current.db:
                current.db.execute('UPDATE runs SET ended=?,outcome=?,stop_reason=? WHERE run_id=?',
                                   (utc(), 'budget_exhausted' if current.reason and 'exhausted' in current.reason else
                                    current.outcome if outcome == 'success' else outcome,
                                    current.reason, current.id))
        finally:
            ACTIVE.reset(token)


def classify(error):
    if isinstance(error, DeliveryBudgetExhausted):
        return 'yielded'
    if isinstance(error, TaskBudgetExhausted):
        return 'task_budget_exhausted'
    if isinstance(error, BudgetExhausted):
        return 'budget_exhausted'
    if isinstance(error, subprocess.TimeoutExpired):
        return 'timeout'
    if type(error).__name__ == 'ContributionPaused':
        return 'paused_policy'
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return 'interrupted'
    from failures import category
    return category(error)


@contextlib.contextmanager
def stage(name, task=None):
    current = ACTIVE.get()
    if current is None:
        yield
        return
    current.remaining()
    identifier, started = uuid.uuid4().hex, time.monotonic()
    previous_task = current.task
    current.switch_task(task or previous_task)
    # Orchestration spans are inclusive observations, not additional stage deadlines.
    orchestration = name in ('auto', 'task', 'analyze', 'research_batch')
    current.deadlines.append(float('inf') if orchestration else
                             started + current.p.config.get('timeouts', {}).get('stage_seconds', 2400))
    with current.db:
        current.db.execute('INSERT INTO stages(stage_id,run_id,task,name,started,outcome) VALUES (?,?,?,?,?,?)',
                           (identifier, current.id, current.task, name, utc(), 'running'))
    outcome, reason = 'success', None
    try:
        yield
        current.remaining()
    except BaseException as error:
        outcome, reason = classify(error), str(error) if isinstance(error, BudgetExhausted) else type(error).__name__
        if not isinstance(error, (TaskBudgetExhausted, DeliveryBudgetExhausted)):
            current.outcome = outcome
            current.reason = current.reason or reason
        raise
    finally:
        with current.db:
            current.db.execute('UPDATE stages SET ended=?,elapsed=?,outcome=?,reason=? WHERE stage_id=?',
                               (utc(), time.monotonic() - started, outcome, reason, identifier))
        current.deadlines.pop()
        current.switch_task(previous_task)
        current.checkpoint()


def observed(name, task=False):
    def decorate(function):
        @functools.wraps(function)
        def wrapper(self, *args, **kwargs):
            pipeline = getattr(self, 'p', self)
            with run(pipeline, name), stage(name, args[0] if task and args else None):
                return function(self, *args, **kwargs)
        return wrapper
    return decorate


def model_call(channel, review=None):
    def decorate(function):
        @functools.wraps(function)
        def wrapper(self, task_id, purpose, *args, **kwargs):
            is_review = channel == 'hermes' if review is None else review
            with run(self.p, 'model'), stage('review' if is_review else 'model', task_id):
                current = ACTIVE.get()
                model = self.p.config.get('review_model') or self.p.config.get('model', 'tokenking/devin/swe-2')
                if not is_review:
                    model = self.p.config.get('model', 'tokenking/devin/swe-2')
                identifier = current.reserve_call(task_id, purpose, model, channel)
                previous_call = current.call_id
                current.call_id = identifier
                outcome = 'success'
                try:
                    return function(self, task_id, purpose, *args, **kwargs)
                except BaseException as error:
                    outcome = classify(error)
                    if outcome in ('network', 'rate_limit', 'certificate', 'service_unavailable', 'timeout'):
                        current.channel_failures[channel] = current.channel_failures.get(channel, 0) + 1
                    raise
                finally:
                    with self.db:
                        self.db.execute('UPDATE model_calls SET ended=?,outcome=? WHERE call_id=?', (utc(), outcome, identifier))
                    current.call_id = previous_call
        return wrapper
    return decorate


def record_usage(stream, channel):
    """Read native token counters only. Provider price estimates/zero placeholders are not bills."""
    current = ACTIVE.get()
    if not current or not current.call_id:
        return
    records, seen = [], set()
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if channel == 'opencode' and event.get('type') == 'step_finish':
            part = event.get('part', {})
            identity = part.get('id')
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            tokens = part.get('tokens')
            if isinstance(tokens, dict):
                cache = tokens.get('cache') or {}
                records.append((tokens.get('input'), tokens.get('output'), cache.get('read', 0), cache.get('write', 0)))
        elif channel == 'hermes' and event.get('type') == 'result':
            tokens = event.get('tokens')
            if isinstance(tokens, dict):
                records = [(tokens.get('input'), tokens.get('output'), tokens.get('cache_read', 0), tokens.get('cache_write', 0))]
    if records and all(type(value) is int and value >= 0 for record in records for value in record):
        with current.db:
            current.db.execute('UPDATE model_calls SET input_tokens=?,output_tokens=? WHERE call_id=?',
                               (sum(r[0] + r[2] + r[3] for r in records), sum(r[1] for r in records), current.call_id))
        if channel == 'opencode':
            current.count('reported_provider_steps', len(records))


def timed_command(function):
    @functools.wraps(function)
    def wrapper(args, *positional, **kwargs):
        current = ACTIVE.get()
        if current is None:
            return function(args, *positional, **kwargs)
        requested = kwargs.get('timeout') or current.p.config.get('timeouts', {}).get('command_seconds', 180)
        remaining = current.remaining()
        kwargs['timeout'] = min(requested, remaining)
        try:
            result = function(args, *positional, **kwargs)
            current.count('commands')
            if result.returncode:
                current.count('command_failures')
            current.remaining()
            return result
        except subprocess.TimeoutExpired:
            current.count('timeouts')
            if remaining <= requested:
                # Re-check which deadline actually expired. A single task or
                # stage limit must not turn into whole-run exhaustion.
                current.remaining()
            raise
    return wrapper


def metrics(path, limit=10):
    """A separate read-only connection: no migration, network or model invocation."""
    db = sqlite3.connect('file:' + path.resolve().as_posix() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'runs' not in tables:
            return {'runs': [], 'note': 'No observations yet; legacy database unchanged'}
        runs = [dict(r) for r in db.execute('SELECT * FROM runs ORDER BY started DESC LIMIT ?', (limit,))]
        for item in runs:
            item['counters'] = json.loads(item['counters'])
            item['stages'] = [dict(r) for r in db.execute('SELECT * FROM stages WHERE run_id=? ORDER BY started', (item['run_id'],))]
            item['usage'] = dict(db.execute('SELECT count(*) calls,count(input_tokens) token_known_calls,sum(input_tokens) input_tokens,sum(output_tokens) output_tokens,'
                                           'sum(actual_cost_usd) actual_cost_usd,count(actual_cost_usd) cost_known_calls '
                                           'FROM model_calls WHERE run_id=?', (item['run_id'],)).fetchone())
            item['models'] = [dict(r) for r in db.execute('SELECT model,channel,count(*) calls FROM model_calls '
                                                         'WHERE run_id=? GROUP BY model,channel', (item['run_id'],))]
            item['usage']['token_status'] = ('unavailable' if not item['usage']['token_known_calls'] else
                                            'complete' if item['usage']['token_known_calls'] == item['usage']['calls'] else 'partial')
            if item['usage']['cost_known_calls'] != item['usage']['calls'] or not item['usage']['calls']:
                item['usage']['actual_cost_usd'] = None
        stages = [dict(r) for r in db.execute('SELECT name,outcome,reason,count(*) attempts,avg(elapsed) average_seconds FROM stages GROUP BY name,outcome,reason')]
        queues = [dict(r) for r in db.execute('SELECT category,queue,count(*) candidates FROM research_candidates GROUP BY category,queue')]
        tasks = [dict(r) for r in db.execute('SELECT state,count(*) tasks FROM tasks GROUP BY state')]
        quality = quality_metrics(db, tables)
        from research_scope import report_pending
        scope_reviews = report_pending(db) if {'automation', 'tasks'} <= tables else []
        issue_refreshes = ([dict(row) for row in db.execute(
            'SELECT status,count(*) count FROM issue_refresh_queue GROUP BY status')]
                           if 'issue_refresh_queue' in tables else [])
        return {'runs': runs, 'stage_outcomes_all_time': stages, 'candidate_queues': queues,
                'task_states': tasks, 'quality_all_time': quality, 'pr_maintenance': maintenance_gaps(db),
                'scope_overlap_reviews': scope_reviews, 'issue_refresh_queue': issue_refreshes,
                'cost_status': 'unavailable: adapters do not report reliable billable usage',
                'cost_caps_enforced': False, 'note': 'Running records without ended are incomplete, never assumed successful. Stage attempts are not unique contributions.'}
    finally:
        db.close()


def maintenance_gaps(db):
    """Report observed open PRs without a live task link; never adopt a patch.

    Followup observations include manually published PRs, whereas automation
    only knows explicitly registered PR numbers. Keep these scopes separate
    from the submitted-PR quality metrics and retain observation timestamps.
    """
    from scheduling import TERMINAL
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {'open_observed_prs': 0, 'gap_count': 0, 'gaps': [],
              'pending_action_count': 0, 'pending_actions': [],
              'scope': 'Last locally observed open PRs; not a fresh upstream check or an automatic repair queue.'}
    if 'pr_observations' not in tables:
        return result
    linked = {}
    if {'tasks', 'automation'} <= tables:
        for task, repo, state, raw in db.execute(
                'SELECT tasks.id,tasks.repo,tasks.state,automation.data '
                'FROM tasks JOIN automation ON automation.task=tasks.id'):
            data = json.loads(raw)
            number = data.get('pr_number')
            if type(number) is int:
                linked.setdefault((repo.casefold(), number), []).append(
                    {'task': task, 'state': state, 'stage': data.get('stage', state)})
                merge = data.get('pr_merge') or {}
                pending = data.get('maintenance_pending') or {}
                if state not in TERMINAL and data.get('stage', state) not in TERMINAL and (merge.get('state') in ('pending', 'merging', 'resolved_unverified',
                                          'identity_unavailable', 'needs_verification_recipe', 'target_changed')
                        or pending.get('reason') == 'publication_identity_changed'
                        or data.get('maintenance_observation_only')):
                    result['pending_actions'].append({'repo': repo, 'number': number, 'task': task,
                        'reason': pending.get('reason') or ('merge_conflict' if merge else 'verification_recipe_required'),
                        'state': merge.get('state', 'observation_only'),
                        'identity': merge.get('identity', data.get('pr_observed_identity')),
                        'detail': pending.get('detail')})
    # Repository spelling may have changed case between old observations.
    observations = {}
    merge_observations = {}
    if 'pr_maintenance_observations' in tables:
        merge_observations = {(row[0].casefold(), row[1]): row for row in db.execute(
            'SELECT repo,number,head_sha,base_sha,base_ref,mergeable,merge_state,observed_at,'
            + ('target_base_sha' if 'target_base_sha' in {r[1] for r in db.execute('PRAGMA table_info(pr_maintenance_observations)')} else 'NULL')
            + ' FROM pr_maintenance_observations')}
    for row in db.execute('SELECT repo,number,head_sha,state,ci_state,maintainer_response,observed_at '
                          'FROM pr_observations ORDER BY observed_at'):
        observations[(row[0].casefold(), row[1])] = row
    from scheduling import TERMINAL
    for key, (repo, number, sha, state, ci, response, observed_at) in sorted(observations.items()):
        if state != 'OPEN':
            continue
        result['open_observed_prs'] += 1
        tasks = linked.get(key, [])
        merge = merge_observations.get(key)
        if merge and merge[2] == sha and (merge[5] == 'CONFLICTING' or merge[6] == 'DIRTY'):
            if not any(x['repo'].casefold() == key[0] and x['number'] == number for x in result['pending_actions']):
                result['pending_actions'].append({'repo': repo, 'number': number,
                    'reason': 'merge_conflict', 'state': 'observed', 'linked_tasks': tasks,
                    'head_sha': sha, 'base_sha': merge[8], 'metadata_base_sha': merge[3], 'observed_at': merge[7]})
        if any(task['state'] not in TERMINAL and task['stage'] not in TERMINAL for task in tasks):
            continue
        signals = []
        if ci in ('failed', 'approval_required', 'unknown'):
            signals.append('ci_' + ci)
        if response:
            signals.append('maintainer_response_observed')
        result['gaps'].append({'repo': repo, 'number': number,
                              'url': f'https://github.com/{repo}/pull/{number}',
                              'head_sha': sha, 'ci_state': ci, 'observed_at': observed_at,
                              'reason': 'inactive_linked_task' if tasks else 'untracked_pr',
                              'linked_tasks': tasks, 'signals': signals,
                              'next_step': 'Verify ownership, current head/base and local patch identity before registering or restoring maintenance; inspect CI and feedback.'})
    result['gap_count'] = len(result['gaps'])
    result['pending_action_count'] = len(result['pending_actions'])
    return result


def observe_pr_identity(db, repo, pr):
    """Persist metadata without claiming a fresh CI, discussion or approval read."""
    if not any(key in pr for key in ('mergeable', 'mergeStateStatus', 'baseRefOid')):
        return
    with db:
        db.execute('INSERT INTO pr_maintenance_observations '
                   '(repo,number,head_sha,base_sha,base_ref,mergeable,merge_state,observed_at,target_base_sha) VALUES (?,?,?,?,?,?,?,?,?) '
                   'ON CONFLICT(repo,number) DO UPDATE SET head_sha=excluded.head_sha,base_sha=excluded.base_sha,'
                   'base_ref=excluded.base_ref,mergeable=excluded.mergeable,merge_state=excluded.merge_state,observed_at=excluded.observed_at,target_base_sha=excluded.target_base_sha',
                   (repo, pr['number'], pr.get('headRefOid'), pr.get('baseRefOid'), pr.get('baseRefName'),
                    pr.get('mergeable'), pr.get('mergeStateStatus'), utc(), pr.get('targetBaseOid')))


def observe_pr(db, repo, pr):
    comments = sum((pr.get(k, []) for k in ('comments', 'inline_comments', 'reviews')), [])
    from repository_policy import human_maintainer_comments
    response = bool(human_maintainer_comments(comments))
    approvals = [c for c in comments if c.get('state') == 'APPROVED'
                 and c.get('author_association') in ('OWNER', 'MEMBER', 'COLLABORATOR')
                 and (c.get('user') or {}).get('type') == 'User'
                 and (c.get('user') or {}).get('login')
                 and not c['user']['login'].lower().endswith('[bot]')
                 and c.get('commit_id') and c['commit_id'] == pr.get('headRefOid')]
    observe_pr_identity(db, repo, pr)
    with db:
        db.execute('INSERT INTO pr_observations VALUES (?,?,?,?,?,?,?) ON CONFLICT(repo,number) DO UPDATE SET '
                   'head_sha=excluded.head_sha,state=excluded.state,ci_state=excluded.ci_state,'
                   'maintainer_response=max(pr_observations.maintainer_response,excluded.maintainer_response),'
                   'observed_at=excluded.observed_at',
                   (repo, pr['number'], pr.get('headRefOid'), pr['state'],
                    pr.get('ci', {}).get('state', 'unknown'), int(response), utc()))
        db.execute('INSERT INTO pr_acceptance_observations VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(repo,number) DO UPDATE SET '
                   'head_sha=excluded.head_sha,review_decision=excluded.review_decision,human_approval=excluded.human_approval,'
                   'merged_at=excluded.merged_at,state=excluded.state,observed_at=excluded.observed_at',
                   (repo, pr['number'], pr.get('headRefOid'), pr.get('reviewDecision'), int(bool(approvals)),
                    pr.get('mergedAt'), pr['state'], utc()))


def quality_metrics(db, tables):
    def ratio(numerator, denominator, scope):
        return {'numerator': numerator, 'denominator': denominator,
                'ratio': numerator / denominator if denominator else None, 'scope': scope}
    rows = [dict(r) for r in db.execute('SELECT * FROM research_candidates')]
    matrices = [json.loads(r['matrix']) for r in rows]
    count = len(rows)
    evidence = sum(any(e.get('file_type') in ('source', 'test') for e in m.get('evidence', [])) for m in matrices)
    multi = sum(len(m.get('peer_repositories', [])) >= 3 and
                all(v['status'] != 'unknown' for v in m.get('feature_status', {}).values()) for m in matrices)
    result = {'researched_unique_candidates': count,
              'source_evidence': ratio(evidence, count, 'unique indexed candidates; legacy evidence not backfilled'),
              'feature_candidates': ratio(sum(r['category'] == 'feature' for r in rows), count, 'unique indexed candidates'),
              'bug_candidates': ratio(sum(r['category'] == 'bug' for r in rows), count, 'unique indexed candidates'),
              'multi_peer_complete': ratio(multi, count, 'at least three peers with no unknown statuses'),
              'policy_paused': ratio(sum(r['queue'] == 'PAUSED_POLICY' for r in rows), count, 'current candidate queues'),
              'needs_maintainer': ratio(sum(r['queue'] == 'NEEDS_MAINTAINER_CONFIRMATION' for r in rows), count, 'current candidate queues')}
    for label, phase in [('implementation', 'implementation'), ('baseline_reproduction', 'reproduction'),
                         ('patch_tests', 'testing'), ('automated_review', 'auto_review'), ('pr_creation', 'pr_publication')]:
        attempted = db.execute('SELECT count(DISTINCT task) FROM stages WHERE name=?', (phase,)).fetchone()[0]
        completed = db.execute("SELECT count(DISTINCT task) FROM stages WHERE name=? AND outcome='success'", (phase,)).fetchone()[0]
        if label == 'pr_creation':
            completed = (db.execute("SELECT count(DISTINCT task) FROM automation WHERE json_extract(data,'$.pr_number') IS NOT NULL "
                                    "AND task IN (SELECT task FROM stages WHERE name='pr_publication')").fetchone()[0]
                         if 'automation' in tables else 0)
        result[label] = ratio(completed, attempted, 'distinct tasks with observed stage attempts; not upstream acceptance')
    published, merged, observed_passed, responses, pending, approved = 0, 0, 0, 0, 0, 0
    if 'automation' in tables:
        contributions = db.execute("SELECT tasks.repo,tasks.state,json_extract(automation.data,'$.pr_number') number "
                                   "FROM tasks JOIN automation ON task=tasks.id WHERE json_extract(automation.data,'$.pr_number') IS NOT NULL").fetchall()
        unique = {(r['repo'], r['number']): r['state'] for r in contributions}
        published = len(unique)
        for (repo, number), state in unique.items():
            observation = db.execute('SELECT * FROM pr_observations WHERE repo=? AND number=?', (repo, number)).fetchone()
            merged += state == 'MERGED'
            observed_passed += bool(observation and observation['ci_state'] == 'observed_checks_passed')
            responses += bool(observation and observation['maintainer_response'])
            if 'pr_acceptance_observations' in tables:
                acceptance = db.execute('SELECT human_approval,head_sha FROM pr_acceptance_observations WHERE repo=? AND number=?', (repo, number)).fetchone()
                approved += bool(acceptance and acceptance['human_approval'] and observation and acceptance['head_sha'] == observation['head_sha'])
            pending += state not in ('MERGED', 'CLOSED')
    result.update(submitted_unique_prs=published, merged_prs=merged, pending_observation_prs=pending,
                  human_review_approved_current_head=ratio(approved, published, 'submitted PRs; exact current head human maintainer review, not merge'),
                  ci_observed_passed=ratio(observed_passed, published, 'submitted PRs; unobserved is not passed; branch protection not inferred'),
                  maintainer_response=ratio(responses, published, 'submitted PRs with verified maintainer association; not approval'),
                  merge_rate=ratio(merged, published, 'submitted PRs; open PRs remain pending'))
    counters = [json.loads(r[0]) for r in db.execute('SELECT counters FROM runs')]
    attempts = sum(c.get('researched_candidates', 0) for c in counters)
    result['duplicate_research_attempts'] = ratio(sum(c.get('duplicate_candidates', 0) for c in counters), attempts, 'research attempts, not unique candidates')
    result['insufficient_evidence_attempts'] = ratio(sum(c.get('insufficient_evidence', 0) for c in counters), attempts, 'research attempts, not unique candidates')
    for label, clause in [('qualified_candidate', "queue IN ('READY_TO_IMPLEMENT','NEEDS_MAINTAINER_CONFIRMATION')"),
                          ('submitted_pr', "task IN (SELECT task FROM automation WHERE json_extract(data,'$.pr_number') IS NOT NULL)" if 'automation' in tables else '0')]:
        selected = [r[0] for r in db.execute('SELECT DISTINCT task FROM research_candidates WHERE ' + clause)]
        durations = []
        for task in selected:
            # Task spans are inclusive; never sum nested phases and double count time.
            # Research nested inside task is excluded using interval containment.
            seconds = db.execute("SELECT sum(s.elapsed) FROM stages s WHERE s.task=? AND s.name IN ('task','research') "
                                 "AND NOT EXISTS(SELECT 1 FROM stages p WHERE p.task=s.task AND p.run_id=s.run_id AND p.name='task' "
                                 "AND s.name='research' AND p.started<=s.started AND (p.ended IS NULL OR p.ended>=s.ended))", (task,)).fetchone()[0]
            if seconds is not None:
                durations.append(seconds)
        result[label + '_mean_seconds'] = sum(durations) / len(durations) if durations else None
        result[label + '_actual_cost_usd'] = None
    return result
