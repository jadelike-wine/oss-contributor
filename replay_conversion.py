"""Replay complete saved queues without models, network access or publication.

Compatibility/recovery uses an independent SQLite backup. Queue comparison runs
the historical and current controller in separate Python interpreters; stage
bodies are replaced only for the explicitly labelled dispatch quota probe.
"""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
HISTORY = ('tasks', 'events', 'automation', 'research_candidates', 'pr_observations')


def digest_rows(db, table, columns=None):
    columns = columns or [row[1] for row in db.execute('PRAGMA table_info("' + table + '")')]
    projection = ','.join('"' + column + '"' for column in columns)
    digest = hashlib.sha256()
    count = 0
    for row in db.execute('SELECT ' + projection + ' FROM "' + table + '" ORDER BY ' + projection):
        digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':')).encode())
        digest.update(b'\n')
        count += 1
    return {'count': count, 'sha256': digest.hexdigest(), 'columns': columns}


def backup(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError('Refusing to overwrite replay history: ' + str(target))
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def compatibility(source, target):
    from scheduling import migrate as migrate_scheduling, reconcile_issue_duplicates
    from telemetry import migrate, recover_interrupted
    from contribute import process_running
    backup(source, target)
    with sqlite3.connect(target) as db:
        db.row_factory = sqlite3.Row
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {table: digest_rows(db, table) for table in HISTORY}
        keys_before = {table: digest_rows(db, table, [next((r[1] for r in db.execute('PRAGMA table_info("' + table + '")') if r[5]), 'rowid')])
                       for table in tables}
        pending = [dict(row) for row in db.execute('SELECT * FROM runs WHERE ended IS NULL')]
        migrate(db)
        migrate_scheduling(db)
        first_links = reconcile_issue_duplicates(db)
        first_aliases = digest_rows(db, 'task_aliases')
        migrate(db)
        migrate_scheduling(db)
        second_links = reconcile_issue_duplicates(db)
        second_aliases = digest_rows(db, 'task_aliases')
        recovered = recover_interrupted(db, process_running, legacy_processes_checked=True)
        recovered_again = recover_interrupted(db, process_running, legacy_processes_checked=True)
        after = {table: digest_rows(db, table, before[table]['columns']) for table in HISTORY}
        keys_after = {table: digest_rows(db, table, keys_before[table]['columns']) for table in tables}
        history_preserved = before == after
        identities_preserved = keys_before == keys_after
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        recovery = [dict(row) for row in db.execute("SELECT run_id,outcome,stop_reason,elapsed FROM runs WHERE outcome='interrupted'")
                    if row['run_id'] in recovered]
        checks = {'integrity': integrity, 'history_unchanged': history_preserved,
                  'all_original_table_row_identities_preserved': identities_preserved,
                  'migration_repeatable': first_aliases == second_aliases and first_links == second_links,
                  'recovery_repeatable': not recovered_again}
        if not all(value is True or value == 'ok' for value in checks.values()):
            raise AssertionError(checks)
        return {'kind': 'real_history_copy_compatibility_and_recovery', 'source': str(source), 'copy': str(target),
                'checks': checks, 'history_before': before, 'history_after': after,
                'original_table_keys_before': keys_before, 'original_table_keys_after': keys_after,
                'duplicate_task_aliases': [dict(row) for row in db.execute('SELECT * FROM task_aliases ORDER BY task')],
                'unfinished_before': pending, 'recovered': recovery,
                'publication': 'No network/model/stage implementation bodies were called; publication history hashes are unchanged.'}


def worker(module_root, workspace, output):
    """Executed in a separate interpreter for each controller revision."""
    sys.path.insert(0, str(module_root))
    from contribute import Pipeline
    from autonomous import Autonomous
    from telemetry import Run, run
    from unittest.mock import patch
    pipeline = Pipeline(workspace)
    pipeline.effective_repositories = lambda contribution=False: list(pipeline.config['repositories'])
    pipeline.require_contribution = lambda *args, **kwargs: None
    pipeline.report = lambda: None
    engine = Autonomous(pipeline)
    config = pipeline.config
    all_tasks = pipeline.db.execute('SELECT count(*) FROM tasks').fetchone()[0]
    with contextlib.redirect_stdout(io.StringIO()):
        rows = engine.candidates()
        candidates = []
        for row in rows:
            task = pipeline.task(row['id'])
            data = engine.load(row['id'])
            indexed = pipeline.db.execute('SELECT queue FROM research_candidates WHERE task=?', (row['id'],)).fetchone()
            candidates.append({'id': row['id'], 'repo': row['repo'], 'number': row['number'],
                               'stage': data['stage'], 'queue': indexed['queue'] if indexed else None,
                               'pr_number': data.get('pr_number'), 'source': task['source']})
        # Whole historical queue is ranked above. This probe isolates the old/new
        # per-run implementation guard, not actual code generation or throughput.
        eligible = [item for item in candidates if item['stage'] in {'REPRODUCE', 'REPAIR', 'PATCH_READY', 'AUTO_REVIEW'}
                    or item['stage'] == 'PLAN' and (item['number'] or item['queue'] == 'READY_TO_IMPLEMENT')]
        requested = workspace / 'quota-inputs.json'
        if requested.exists():
            ids = json.loads(requested.read_text(encoding='utf-8'))
            indexed_candidates = {item['id']: item for item in eligible}
            selected = [indexed_candidates[key] for key in ids]
        else:
            selected = eligible[:config.get('automation', {}).get('max_tasks_per_tick', 1) + 1]
        dispatched = []
        for stage, method in [('PLAN', 'plan'), ('REPRODUCE', 'reproduce'), ('REPAIR', 'repair'),
                              ('PATCH_READY', 'verify'), ('AUTO_REVIEW', 'review')]:
            setattr(engine, method, lambda key, data, stage=stage: dispatched.append({'id': key, 'stage': stage}))
        with patch.object(Run, 'remaining', return_value=5400), run(pipeline, 'history_dispatch_probe'):
            for item in selected:
                engine.advance(item['id'])
    result = {'kind': 'historical_full_queue_and_mock_stage_dispatch_not_live_throughput',
              'all_historical_tasks': all_tasks, 'configured_task_limit': config['automation']['max_tasks_per_tick'],
              'queue_count': len(candidates), 'queue': candidates, 'eligible_implementation_count': len(eligible),
              'quota_probe_inputs': selected, 'quota_probe_dispatched': dispatched,
              'network_calls': 0, 'model_calls': 0, 'publications': 0,
              'health_policy_note': 'Both queue replays use the same original configured repository set; no live health inference is made.',
              'quota_probe_note': 'Stage bodies are replaced with local recording functions. Time check is stubbed for historical replay; no production budget is changed.'}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    pipeline.db.close()


def replay(source, output, revision):
    output.mkdir(parents=True, exist_ok=True)
    recovery = compatibility(source, output / 'history-recovered.db')
    config_text = subprocess.check_output(['git', 'show', revision + ':config.json'], cwd=ROOT, text=True, encoding='utf-8')
    old_modules = output / 'old-controller'
    old_modules.mkdir()
    modules = ('autonomous.py', 'contribute.py', 'telemetry.py', 'research.py', 'feature_matrix.py',
               'repository_policy.py', 'repository_catalog.py', 'ci_status.py')
    for module in modules:
        text = subprocess.check_output(['git', 'show', revision + ':' + module], cwd=ROOT, text=True, encoding='utf-8')
        (old_modules / module).write_text(text, encoding='utf-8')
    results = {}
    for label, module_root in [('before', old_modules), ('after', ROOT)]:
        workspace = output / (label + '-workspace')
        backup(source, workspace / 'state/tasks.db')
        (workspace / 'config.json').write_text(config_text, encoding='utf-8')
        if label == 'after':
            from scheduling import reconcile_issue_duplicates
            with sqlite3.connect(workspace / 'state/tasks.db') as db:
                db.row_factory = sqlite3.Row
                reconcile_issue_duplicates(db)
            (workspace / 'quota-inputs.json').write_text(json.dumps([item['id'] for item in results['before']['quota_probe_inputs']]), encoding='utf-8')
        target = output / (label + '-queue.json')
        result = subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--worker',
                                 str(module_root), str(workspace), str(target)], cwd=workspace,
                                capture_output=True, text=True, encoding='utf-8', timeout=180)
        (output / (label + '-worker.log')).write_text(result.stdout + result.stderr, encoding='utf-8')
        if result.returncode:
            raise RuntimeError(label + ' replay failed; see worker log')
        results[label] = json.loads(target.read_text(encoding='utf-8'))
    from scheduling import canonical_repo, stable_focus, existing_hunt
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        groups = {}
        for row in db.execute("SELECT * FROM tasks WHERE source='hunt'"):
            focus = json.loads(row['payload']).get('focus', '')
            groups.setdefault((canonical_repo(row['repo']), stable_focus(focus)), []).append(dict(row))
        repeats = []
        for (repo, focus), rows in groups.items():
            if len(rows) < 2 or not any('Research batch ' in json.loads(row['payload']).get('focus', '') for row in rows):
                continue
            next_focus = focus + ' Research batch 2026-W45'
            old_id = rows[0]['repo'].replace('/', '--') + '--hunt-' + hashlib.sha256(next_focus.encode()).hexdigest()[:12]
            repeats.append({'repo': repo, 'focus': focus, 'historical_tasks': [row['id'] for row in rows],
                            'next_week_before_would_create': not bool(db.execute('SELECT 1 FROM tasks WHERE id=?', (old_id,)).fetchone()),
                            'next_week_after_reuses': existing_hunt(db, repo, next_focus)})
    report = {'baseline_revision': revision, 'scope': 'Every original task retained; no production state or upstream was modified.',
              'compatibility': recovery,
              'queue_comparison': {label: {key: value for key, value in result.items() if key != 'queue'} for label, result in results.items()},
              'full_queue_artifacts': {label: str(output / (label + '-queue.json')) for label in results},
              'weekly_research_replay': repeats,
              'limitations': ['Queue ordering and mocked dispatch are historical replay, not live conversion or throughput.',
                              'No code patch, verification pass, maintainer acceptance or merge is produced by this replay.',
                              'Cache savings and actual failure recovery latency require separate real bounded runs.']}
    (output / 'replay-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'checks': recovery['checks'], 'tasks_preserved': recovery['history_before']['tasks']['count'],
                      'aliases': len(recovery['duplicate_task_aliases']), 'recovered_runs': len(recovery['recovered']),
                      'queue_counts': {key: value['queue_count'] for key, value in results.items()},
                      'dispatch_counts': {key: len(value['quota_probe_dispatched']) for key, value in results.items()},
                      'repeated_weekly_groups': len(repeats)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=ROOT / 'runs/conversion-20261031/baseline.db')
    parser.add_argument('--output', type=Path, default=ROOT / 'runs/conversion-20261031/history-replay')
    parser.add_argument('--revision', default='ab5ed20')
    parser.add_argument('--worker', nargs=3)
    arguments = parser.parse_args()
    if arguments.worker:
        worker(*map(Path, arguments.worker))
    else:
        replay(arguments.source.resolve(), arguments.output.resolve(), arguments.revision)
