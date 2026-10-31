"""Bounded live collection/scheduling validation on a full historical DB copy.

This entrypoint has no implementation/publication dispatch. It measures real
read-only GitHub scans and queue decisions, not model or repair throughput.
"""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import time

from audit_conversion import snapshot
from autonomous import Autonomous
from contribute import Pipeline, ContributionPaused, gh, now, save
from scheduling import reconcile_issue_duplicates, task_lane, RunTaskBudget
from telemetry import run


def initialize(source, target):
    if target.exists():
        raise ValueError('Use a new validation directory; existing evidence is never overwritten')
    target.mkdir(parents=True)
    for name in ('config.json', 'repository-catalog.json'):
        shutil.copy2(source / name, target / name)
    snapshot(source / 'state/tasks.db', target / 'state/tasks.db')
    shutil.copy2(source / 'state/repositories.json', target / 'state/repositories.json')
    shutil.copytree(source / 'runs/snapshots', target / 'runs/snapshots')


def validate(source, target, rounds=3, scan_budget=12, resume=False):
    if not resume:
        initialize(source, target)
    elif not (target / 'state/tasks.db').exists():
        raise ValueError('Resume requires an existing validation snapshot')
    p = Pipeline(target)
    assert not p.allow_external_writes
    reconcile_issue_duplicates(p.db)
    engine = Autonomous(p)
    results = json.loads((target / 'results.json').read_text(encoding='utf-8')) if (target / 'results.json').exists() else []
    offset = len(results)
    for index in range(rounds):
        start = time.monotonic()
        with run(p, 'live_read_only_validation') as current:
            before = p.db.execute('SELECT count(*) FROM tasks').fetchone()[0]
            p.scan(max_repositories=scan_budget)
            scan = json.loads((target / 'runs/scan-summary.json').read_text(encoding='utf-8'))
            candidates = engine.candidates()
            budget, selected = RunTaskBudget(p.config), []
            for row in candidates:
                task = p.task(row['id'])
                data = engine.load(row['id'])
                queue = p.db.execute('SELECT queue FROM research_candidates WHERE task=?', (row['id'],)).fetchone()
                lane = task_lane(task, data, queue['queue'] if queue else None)
                if budget.admit(row['id'], lane):
                    selected.append({'id': row['id'], 'repo': row['repo'], 'stage': data['stage'], 'lane': lane})
            # Two audited real candidates are freshly checked each round, independent of cached scans.
            observations = []
            for repo, number in [('mermaid-js/mermaid', 7953), ('agno-agi/agno', 10887)]:
                item = {'repo': repo, 'number': number, 'observed_at': now()}
                try:
                    issue = gh('api', f'repos/{repo}/issues/{number}')
                    item.update(state=issue['state'], labels=[x['name'] for x in issue['labels']], updated_at=issue['updated_at'])
                    task = {'id': 'validation:' + repo, 'repo': repo, 'number': number}
                    p.require_contribution(task, publishing=True)
                    item['policy'] = 'eligible_for_further_validation'
                except ContributionPaused as error:
                    item.update(policy='paused', reason=str(error))
                except (RuntimeError, ValueError, OSError, KeyError) as error:
                    item.update(policy='unknown', reason=str(error))
                observations.append(item)
            item = {'round': offset + index + 1, 'run_id': current.id, 'external_writes_allowed': False,
                    'mode': 'live_collection_and_queue_observation_only', 'tasks_before': before,
                    'tasks_after': p.db.execute('SELECT count(*) FROM tasks').fetchone()[0],
                    'candidate_count': len(candidates), 'selected': selected, 'scan': scan,
                    'live_candidate_observations': observations, 'elapsed_seconds': time.monotonic() - start,
                    'implementation_tasks_executed': 0, 'patches_verified': 0,
                    'integrity': p.db.execute('PRAGMA integrity_check').fetchone()[0]}
            results.append(item)
            save(target / f'round-{offset + index + 1}.json', item)
            save(target / 'results.json', results)
            print(json.dumps({k: item[k] for k in ('round', 'tasks_before', 'tasks_after', 'candidate_count', 'elapsed_seconds', 'integrity')}) , flush=True)
    p.db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--scan-budget', type=int, default=12)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.rounds <= 3 or not 1 <= args.scan_budget <= 94:
        parser.error('Bound rounds to 1..3 and scan budget to 1..94')
    validate(args.source, args.output, args.rounds, args.scan_budget, args.resume)
