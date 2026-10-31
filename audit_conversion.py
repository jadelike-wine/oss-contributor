"""Read-only consistent history audit; never starts the contribution controller."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3


def snapshot(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError('Refusing to replace an existing history snapshot')
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)


def audit(path, since='2026-10-19'):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = lambda sql, args=(): [dict(r) for r in db.execute(sql, args)]
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts = {t: db.execute('SELECT count(*) FROM "' + t + '"').fetchone()[0] for t in tables}
        completed = rows('SELECT * FROM runs WHERE started>=? AND ended IS NOT NULL', (since,))
        stages = rows('SELECT * FROM stages WHERE run_id IN (SELECT run_id FROM runs WHERE started>=? AND ended IS NOT NULL)', (since,))
        # Union intervals per run: parent and child spans are never added twice.
        def union_seconds(names):
            total = 0.0
            for run in completed:
                intervals = sorted((dt.datetime.fromisoformat(s['started']), dt.datetime.fromisoformat(s['ended']))
                                   for s in stages if s['run_id'] == run['run_id'] and s['name'] in names and s['ended'])
                end = None
                for a, b in intervals:
                    start = max(a, end) if end else a
                    total += max(0, (b - start).total_seconds())
                    end = max(b, end) if end else b
            return total
        return {
            'snapshot': str(path.resolve()), 'since': since,
            'integrity': db.execute('PRAGMA integrity_check').fetchone()[0], 'table_counts': counts,
            'completed_runs': len(completed), 'completed_run_seconds': sum(r['elapsed'] for r in completed),
            'health_scan_research_union_seconds': union_seconds({'health', 'scan', 'research_batch', 'research'}),
            'stage_names': sorted({s['name'] for s in stages}),
            'stage_summary': rows('SELECT name,count(*) count,count(DISTINCT task) tasks,sum(elapsed) inclusive_seconds FROM stages WHERE run_id IN (SELECT run_id FROM runs WHERE started>=? AND ended IS NOT NULL) GROUP BY name', (since,)),
            'running': rows("SELECT * FROM runs WHERE ended IS NULL OR outcome='running'"),
            'locks': rows('SELECT * FROM locks'),
            'duplicate_issues': rows('SELECT lower(repo) repo,number,count(*) count,group_concat(id) ids FROM tasks WHERE number IS NOT NULL GROUP BY lower(repo),number HAVING count(*)>1'),
            'states': rows('SELECT state,count(*) count FROM tasks GROUP BY state'),
            'queues': rows('SELECT queue,count(*) count FROM research_candidates GROUP BY queue'),
            'candidate_history': rows("SELECT t.id,t.repo,t.number,t.state,t.workspace,t.base_sha,a.data FROM tasks t LEFT JOIN automation a ON a.task=t.id WHERE t.repo='mermaid-js/mermaid' OR (lower(t.repo)='agno-agi/agno' AND t.number=10887)"),
            'research_batches': rows("SELECT repo,count(*) count FROM tasks WHERE source='hunt' GROUP BY lower(repo) HAVING count(*)>1"),
            'failures': rows("SELECT name,outcome,reason,count(*) count FROM stages WHERE started>=? AND outcome!='success' GROUP BY name,outcome,reason", (since,)),
        }


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source', type=Path, default=Path('state/tasks.db'))
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / 'baseline.db'
    snapshot(args.source, target)
    result = audit(target)
    (args.output / 'baseline.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('integrity', 'table_counts', 'completed_runs', 'completed_run_seconds', 'health_scan_research_union_seconds', 'locks', 'duplicate_issues')}, ensure_ascii=False, indent=2))
