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
import sqlite3
import subprocess
import time
import uuid

ACTIVE = contextvars.ContextVar('oss_run', default=None)


class BudgetExhausted(RuntimeError):
    pass


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
    ''')


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
        self.call_id = None
        self.task = None
        self.task_elapsed = {}
        self.task_active_since = self.start
        with self.db:
            self.db.execute('INSERT INTO runs(run_id,entry,started,outcome) VALUES (?,?,?,?)',
                            (self.id, entry, self.started, 'running'))

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
        limits = [self.start + self.options.get('run_seconds', 5400) - current]
        limits += [deadline - current for deadline in self.deadlines]
        if self.task:
            spent = self.task_elapsed.get(self.task, 0) + current - self.task_active_since
            limits.append(self.options.get('task_seconds', 3600) - spent)
        daily = self.db.execute('SELECT coalesce(sum(elapsed),0) FROM runs WHERE started>=? AND run_id!=?',
                                (self.started[:10], self.id)).fetchone()[0]
        limits.append(self.options.get('daily_seconds', 21600) - daily - (current - self.start))
        remaining = min(limits)
        if remaining <= 0:
            self.stop('time_budget_exhausted')
        return remaining

    def switch_task(self, task):
        if task == self.task:
            return
        current = time.monotonic()
        if self.task:
            self.task_elapsed[self.task] = self.task_elapsed.get(self.task, 0) + current - self.task_active_since
        self.task, self.task_active_since = task, current

    def reserve_call(self, task, purpose, model, channel):
        self.remaining()
        checks = [('run_model_calls', 16, 'run_id=?', (self.id,)),
                  ('task_model_calls', 40, 'task=?', (task,)),
                  ('daily_model_calls', None, 'started>=?', (self.started[:10],))]
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
    if isinstance(error, BudgetExhausted):
        return 'budget_exhausted'
    if isinstance(error, subprocess.TimeoutExpired):
        return 'timeout'
    if type(error).__name__ == 'ContributionPaused':
        return 'paused_policy'
    return 'interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'failed'


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
                current.stop('time_budget_exhausted')
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
        return {'runs': runs, 'stage_outcomes_all_time': stages, 'candidate_queues': queues,
                'task_states': tasks, 'quality_all_time': quality,
                'cost_status': 'unavailable: adapters do not report reliable billable usage',
                'cost_caps_enforced': False, 'note': 'Running records without ended are incomplete, never assumed successful. Stage attempts are not unique contributions.'}
    finally:
        db.close()


def observe_pr(db, repo, pr):
    comments = sum((pr.get(k, []) for k in ('comments', 'inline_comments', 'reviews')), [])
    response = any(c.get('author_association') in ('OWNER', 'MEMBER', 'COLLABORATOR')
                   and (c.get('user') or {}).get('type') != 'Bot' for c in comments)
    with db:
        db.execute('INSERT INTO pr_observations VALUES (?,?,?,?,?,?,?) ON CONFLICT(repo,number) DO UPDATE SET '
                   'head_sha=excluded.head_sha,state=excluded.state,ci_state=excluded.ci_state,'
                   'maintainer_response=max(pr_observations.maintainer_response,excluded.maintainer_response),'
                   'observed_at=excluded.observed_at',
                   (repo, pr['number'], pr.get('headRefOid'), pr['state'],
                    pr.get('ci', {}).get('state', 'unknown'), int(response), utc()))


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
    published, merged, observed_passed, responses, pending = 0, 0, 0, 0, 0
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
            pending += state not in ('MERGED', 'CLOSED')
    result.update(submitted_unique_prs=published, merged_prs=merged, pending_observation_prs=pending,
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
