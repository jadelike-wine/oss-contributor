"""Persistent exact-scope reuse and reviewable partial-overlap holds.

Titles and feature keys are never identity evidence. Partial overlap preserves
both candidates and any unique work until the remaining scope is reviewed.
"""
import datetime as dt
import hashlib
import json
import subprocess


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def target_evidence(repo, matrix):
    return sorted({(item['path'], item['sha'], item['quote']) for item in matrix.get('evidence', [])
                   if str(item.get('repo', '')).casefold() == repo.casefold()
                   and item.get('path') and item.get('sha') and item.get('quote')
                   and item.get('file_type') in ('source', 'test')})


def scope_identity(repo, matrix):
    evidence = target_evidence(repo, matrix)
    required = ('target_sha', 'user_scenario', 'affected_files', 'implementation_plan', 'validation_plan')
    if matrix.get('legacy') or not evidence or not all(matrix.get(key) for key in required):
        return None
    return _hash({'repo': repo.casefold(), 'evidence': evidence,
                  **{key: matrix[key] for key in required}})


def pending(data):
    return (data.get('scope_overlap_review', {}).get('state') in ('required', 'waiting_on_prior')
            and not any(data.get(key) for key in ('pr_number', 'pr_create_pending', 'publish_intent', 'push_intent')))


def resolution_digest(data):
    review = data.get('scope_overlap_review', {})
    if review.get('state') != 'resolved':
        return None
    return _hash({key: review.get(key) for key in ('scope', 'reason', 'remaining_scope')})


def overlaps(db, task, matrix):
    current = {(p, quote): sha for p, sha, quote in target_evidence(task['repo'], matrix)}
    if not current:
        return []
    result = []
    for row in db.execute('SELECT c.task,c.matrix,t.state FROM research_candidates c JOIN tasks t ON t.id=c.task '
                          'WHERE lower(c.repo)=lower(?) AND c.task!=? ORDER BY c.task',
                          (task['repo'], task['id'])):
        prior = json.loads(row['matrix'])
        known = {(p, quote): sha for p, sha, quote in target_evidence(task['repo'], prior)}
        shared = sorted(current.keys() & known.keys())
        if not shared:
            continue
        result.append({'task': row['task'], 'prior_state': row['state'], 'exact_scope': scope_identity(task['repo'], matrix) is not None
                       and scope_identity(task['repo'], matrix) == scope_identity(task['repo'], prior),
                       'current_scope': scope_identity(task['repo'], matrix),
                       'prior_scope': scope_identity(task['repo'], prior),
                       'shared_evidence': [{'path': p, 'quote': quote, 'current_sha': current[p, quote],
                                            'prior_sha': known[p, quote]} for p, quote in shared],
                       'current_unique_files': sorted(set(matrix.get('affected_files', [])) - set(prior.get('affected_files', []))),
                       'prior_unique_files': sorted(set(prior.get('affected_files', [])) - set(matrix.get('affected_files', [])))})
    return result


def alias_exact(db, task, matrix):
    """Reuse a reviewed identical scope, retaining both records and all history."""
    identity = scope_identity(task['repo'], matrix)
    if not identity:
        return None
    row = db.execute('SELECT data FROM automation WHERE task=?', (task['id'],)).fetchone()
    data = json.loads(row['data']) if row else {}
    if task.get('source') != 'hunt' or any(data.get(k) for k in ('pr_number', 'pr_create_pending', 'publish_intent', 'push_intent')):
        return None
    for candidate in overlaps(db, task, matrix):
        if not candidate['exact_scope']:
            continue
        prior = db.execute('SELECT t.state,c.queue FROM tasks t JOIN research_candidates c ON c.task=t.id WHERE t.id=?',
                           (candidate['task'],)).fetchone()
        if prior['state'] in ('SKIPPED', 'FAILED', 'CLOSED') or prior['queue'] != 'READY_TO_IMPLEMENT':
            continue
        # Never create a chain/cycle or overwrite a different historical alias.
        if db.execute('SELECT 1 FROM task_aliases WHERE task IN (?,?)', (task['id'], candidate['task'])).fetchone():
            continue
        with db:
            db.execute('INSERT INTO task_aliases VALUES (?,?,?,?,?)',
                       (task['id'], candidate['task'], 'scope:' + identity,
                        'same pinned target evidence, user scenario, affected files, implementation and validation plans',
                        dt.datetime.now(dt.timezone.utc).isoformat()))
        return candidate['task']
    return None


def hold_overlap(db, task_id, prior_task, reason):
    """Record a reviewed partial overlap; no deletion, alias, or approval implied."""
    if not isinstance(reason, str) or len(reason.strip()) < 20:
        raise ValueError('Explain the overlapping behavior and the remaining distinct work')
    task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
    row = db.execute('SELECT matrix FROM research_candidates WHERE task=?', (task_id,)).fetchone()
    if not row or task['source'] != 'hunt':
        raise ValueError('A persisted local research candidate is required')
    matches = [item for item in overlaps(db, task, json.loads(row['matrix'])) if item['task'] == prior_task]
    if not matches or matches[0]['exact_scope']:
        raise ValueError('Partial overlap needs shared exact source evidence and distinct scope')
    if matches[0]['prior_state'] in ('SKIPPED', 'FAILED', 'CLOSED', 'MERGED'):
        raise ValueError('Inactive research cannot block a new partial scope')
    row = db.execute('SELECT data FROM automation WHERE task=?', (task_id,)).fetchone()
    data = json.loads(row['data']) if row else {'stage': 'PLAN', 'attempts': {}}
    if task['state'] in ('PR_OPEN', 'MERGED') or any(data.get(k) for k in ('pr_number', 'pr_create_pending', 'publish_intent', 'push_intent')):
        raise ValueError('Published or pending publication tasks retain their own maintenance')
    resolved = data.get('scope_overlap_review', {})
    old = data.get('scope_overlap_history', [])
    if (resolved.get('state') == 'resolved' and resolved.get('scope') == matches[0]['current_scope']
            and old and old[-1].get('task') == prior_task and old[-1].get('prior_scope') == matches[0]['prior_scope']):
        raise ValueError('Local scope review conflicts with a completed resolution for unchanged material; revise the remaining scope')
    receipt = {'state': 'required', 'at': dt.datetime.now(dt.timezone.utc).isoformat(),
               'reason': reason, **matches[0]}
    data['scope_overlap_review'] = receipt
    with db:
        db.execute('INSERT INTO automation VALUES (?,?) ON CONFLICT(task) DO UPDATE SET data=excluded.data',
                   (task_id, json.dumps(data)))
    return receipt


def resolve_overlap(db, task_id, reason, reviewed_scope):
    """Clear only after explicit review of the current scope; retain the hold."""
    row = db.execute('SELECT data FROM automation WHERE task=?', (task_id,)).fetchone()
    data = json.loads(row['data']) if row else {}
    hold = data.get('scope_overlap_review', {})
    row = db.execute('SELECT repo,matrix FROM research_candidates WHERE task=?', (task_id,)).fetchone()
    actual = scope_identity(row['repo'], json.loads(row['matrix'])) if row else None
    if hold.get('state') not in ('required', 'waiting_on_prior') or not actual or actual != reviewed_scope or len(reason.strip()) < 20:
        raise ValueError('Resolution requires an explanation and the exact current scope identity')
    data.setdefault('scope_overlap_history', []).append(hold)
    data['scope_overlap_review'] = {'state': 'resolved', 'scope': actual, 'reason': reason,
                                   'at': dt.datetime.now(dt.timezone.utc).isoformat()}
    with db:
        db.execute('UPDATE automation SET data=? WHERE task=?', (json.dumps(data), task_id))
    return data['scope_overlap_review']


def review_pending(engine, task_id, data, at=None):
    """One completed verdict per material pair; bounded retries for failed calls."""
    if not pending(data):
        return False
    from telemetry import ACTIVE, BudgetExhausted
    current = ACTIVE.get()
    if current and current.channel_failures.get('hermes', 0) >= 2:
        return False
    prior_id = data['scope_overlap_review']['task']
    materials = []
    for identifier in (task_id, prior_id):
        row = engine.db.execute('SELECT t.repo,t.state,c.matrix FROM tasks t LEFT JOIN research_candidates c ON c.task=t.id '
                                'WHERE t.id=?', (identifier,)).fetchone()
        if not row or not row['matrix']:
            return False
        matrix = json.loads(row['matrix'])
        materials.append({'task': identifier, 'state': row['state'], 'scope': scope_identity(row['repo'], matrix),
                          'matrix': matrix})
    digest = _hash([{key: item[key] for key in ('task', 'state', 'scope')} for item in materials])
    history = data.setdefault('scope_overlap_reviews', [])
    attempts = [item for item in history if item.get('materials_digest') == digest]
    if any(item.get('decision') in ('distinct', 'reuse', 'unknown') and item.get('review') and not item.get('error')
           for item in attempts):
        return False
    now = at or dt.datetime.now(dt.timezone.utc)
    limit = engine.options.get('max_infrastructure_attempts', 3)
    cooldown = engine.options.get('infrastructure_cooldown_seconds', 300)
    if len(attempts) >= limit:
        return False
    if attempts:
        previous = attempts[-1]
        retry_after = previous.get('retry_after')
        due = (dt.datetime.fromisoformat(retry_after) if retry_after else
               dt.datetime.fromisoformat(previous['at']) + dt.timedelta(seconds=min(cooldown * 2 ** (len(attempts) - 1), 3600)))
        if now < due:
            return False
    attempt = len(attempts) + 1
    receipt = {'materials_digest': digest, 'at': now.isoformat(), 'decision': 'started', 'attempt': attempt,
               'retry_after': (now + dt.timedelta(seconds=min(cooldown * 2 ** (attempt - 1), 3600))).isoformat()}
    history.append(receipt)
    engine.store(task_id, data)  # A crash cannot cause an unbounded replay.
    call_count = engine.db.execute("SELECT count(*) FROM model_calls WHERE task=? AND purpose='scope-overlap-review'", (task_id,)).fetchone()[0]
    if current:
        current.count('scope_overlap_reviews')
    try:
        answer = engine.judge(task_id, 'scope-overlap-review', {
            'gate': 'Reconsider a partial local overlap using the current pinned source evidence and task states. '
                    'Return scope_decision=distinct, reuse, or unknown and a concrete reason. Titles are not proof. '
                    'Use distinct with approved=true only if there is independently useful, bounded remaining work; '
                    'also supply remaining_scope explaining the shared work to exclude and the independent behavior/tests. '
                    'Use reuse with approved=false when active prior work should finish first. A failed/skipped/closed '
                    'prior task is not itself a permanent blocker. Use unknown with approved=false if evidence is insufficient. '
                    'This gate only reopens normal planning; it does not approve implementation, publication, upstream policy, '
                    'baseline tests or maintainer acceptance. Never discard unique work or invent an equivalence.',
            'hold': data['scope_overlap_review'], 'candidates': materials})
        decision = answer.get('scope_decision')
        reason, remaining = answer.get('reason'), answer.get('remaining_scope')
        if (decision not in ('distinct', 'reuse', 'unknown') or answer.get('approved') is not (decision == 'distinct')
                or not isinstance(reason, str) or len(reason.strip()) < 20
                or decision == 'distinct' and (not isinstance(remaining, str) or len(remaining.strip()) < 20)):
            raise ValueError('Invalid local scope review; require decision, reason and distinct remaining scope')
        receipt.update(decision=decision, review=answer)
        engine.store(task_id, data)
        if decision == 'distinct':
            reason = answer['reason'] + '\nRemaining scope: ' + answer['remaining_scope']
            resolve_overlap(engine.db, task_id, reason, materials[0]['scope'])
            data.clear()
            data.update(engine.load(task_id))
            data['scope_overlap_review'].update(remaining_scope=answer['remaining_scope'], review=answer)
            engine.move(task_id, data, 'PLAN')
        else:
            data['scope_overlap_review']['state'] = 'waiting_on_prior' if decision == 'reuse' else 'required'
            data['scope_overlap_review']['last_decision'] = decision
            data['scope_overlap_review']['last_reason'] = answer['reason']
            engine.store(task_id, data)
    except BudgetExhausted:
        calls_after = engine.db.execute("SELECT count(*) FROM model_calls WHERE task=? AND purpose='scope-overlap-review'", (task_id,)).fetchone()[0]
        if calls_after == call_count:
            history.remove(receipt)  # Budget refused before any model call was reserved.
        else:
            receipt.update(decision='retryable_error', category='budget_exhausted', error='Review interrupted by model/run budget', exhausted=attempt >= limit)
        engine.store(task_id, data)
        raise
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        from failures import category
        diagnostic = str(error)
        for output in (getattr(error, 'stdout', None), getattr(error, 'stderr', None)):
            if isinstance(output, bytes):
                output = output.decode('utf-8', errors='replace')
            if isinstance(output, str):
                diagnostic += '\n' + output[-1500:]
        receipt.update(decision='retryable_error', category=category(error), error=diagnostic, exhausted=attempt >= limit)
        data['scope_overlap_review']['last_decision'] = 'review_error'
        data['scope_overlap_review']['last_reason'] = diagnostic
        engine.store(task_id, data)
    from contribute import save
    save(engine.packet_dir(task_id) / 'scope-overlap-review-receipt.json', receipt)
    engine.p.event(task_id, 'Local scope review: ' + receipt['decision'] + '; material ' + digest)
    return True


def report_pending(db):
    """Read-only diagnostics for metrics and explicit review/recovery commands."""
    result = []
    for row in db.execute('SELECT a.task,a.data,t.repo,t.state FROM automation a JOIN tasks t ON t.id=a.task'):
        data = json.loads(row['data'])
        if pending(data):
            result.append({'task': row['task'], 'repo': row['repo'], 'state': row['state'],
                           'hold': data['scope_overlap_review'], 'reviews': data.get('scope_overlap_reviews', [])})
    return result
