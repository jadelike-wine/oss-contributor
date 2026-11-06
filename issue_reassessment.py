"""Persist bounded refresh requests without changing Issue eligibility.

Enqueueing is a local bookkeeping operation, not evidence that a hold is wrong.
The scanner completes a request only after fresh Issue and PR reads succeed.
"""
import datetime as dt
import json


def enqueue(db, candidates, *, reason, at=None):
    at = at or dt.datetime.now(dt.timezone.utc).isoformat()
    db.execute('''CREATE TABLE IF NOT EXISTS issue_refresh_queue (
        task TEXT PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL,
        reason TEXT NOT NULL, queued_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        last_attempted_at TEXT, retry_after TEXT, last_error TEXT, completed_at TEXT)''')
    added = 0
    for candidate in candidates:
        task = candidate['id']
        row = db.execute('SELECT repo,number,state,holds FROM tasks WHERE id=?', (task,)).fetchone()
        if not row or row['state'] != 'HOLD' or type(row['number']) is not int:
            continue
        if (row['repo'].casefold() != candidate['repo'].casefold()
                or row['number'] != candidate['number']):
            raise ValueError('Historical refresh identity differs from the saved task: ' + task)
        evidence = {'reason': reason, 'previous_holds': json.loads(row['holds']),
                    'relations': candidate.get('relations', []), 'fresh_read_required': True}
        result = db.execute('''INSERT OR IGNORE INTO issue_refresh_queue
            (task,repo,number,reason,queued_at) VALUES (?,?,?,?,?)''',
            (task, row['repo'], row['number'], json.dumps(evidence, ensure_ascii=False), at))
        added += result.rowcount
    return added
