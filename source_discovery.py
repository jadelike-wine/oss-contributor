"""Bounded original-source scopes from existing local clones; never fetch or publish."""
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import subprocess
from urllib.parse import unquote

from contribute import command, now, save
from feature_matrix import evidence_catalog, file_type
from telemetry import BudgetExhausted

MAX_PATHS = 50000
MAX_FILES = 6
MAX_RULES = 6
MAX_CHARACTERS = 12000
MAX_POLICY_CHARACTERS = 60000


def digest(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def local_git(base, *args):
    # Partial clones may have missing objects. Fail closed instead of implicitly
    # downloading blobs or invoking a credential prompt during scope collection.
    return command(['git', '-c', 'protocol.allow=never', *args], cwd=base, timeout=30,
                   env={**os.environ, 'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0'})


def safe_path(path):
    return (isinstance(path, str) and bool(path) and not PurePosixPath(path).is_absolute()
            and '..' not in PurePosixPath(path).parts
            and not any(char in path for char in ('\\', ':', '\n', '\r', '\0')))


def module_name(path):
    stem = PurePosixPath(path).stem
    return re.sub(r'(^test[_-]|[_-]test$|[.]test$|[.]spec$)', '', stem).casefold()


def module_group(path):
    # Match colocated tests or the same module under parallel src/test trees.
    # A coincidentally named test in another package is not nearby evidence.
    structural = {'src', 'lib', 'test', 'tests', 'spec', 'specs', '__tests__',
                  'tests-unit', 'tests-integration'}
    return tuple(p for p in PurePosixPath(path).parts[:-1] if p not in structural)


def scope_options(paths):
    """One executable module plus related tests, never an arbitrary issue title."""
    excluded = {'node_modules', 'vendor', 'third_party', 'dist', 'generated', '.git',
                '.github', '.agents', '.claude', '.codex', '.cursor', '.gemini',
                'examples', 'fixture', 'fixtures', '__fixtures__'}
    eligible = [p for p in paths if safe_path(p) and not set(PurePosixPath(p).parts) & excluded]
    tests = [p for p in eligible if file_type(p) == 'test' and
             (re.search(r'(^test[_-]|[_.-](test|spec)([_.-]|$))', PurePosixPath(p).name, re.I)
              or '__tests__' in PurePosixPath(p).parts)]
    test_set, tests_by_name = set(tests), {}
    for path in tests:
        tests_by_name.setdefault((module_name(path), module_group(path)), []).append(path)
    sources = [p for p in eligible if file_type(p) == 'source' and p not in test_set
               and not PurePosixPath(p).name.startswith(('__init__', 'conftest', 'test-setup'))]
    result = []
    for source in sorted(sources):
        name = module_name(source)
        if len(name) < 3 or name in ('index', 'main', 'config', 'utils', 'setup'):
            continue
        matches = list(tests_by_name.get((name, module_group(source)), []))
        if not matches:
            continue
        parts = PurePosixPath(source).parts[:-1]
        matches.sort(key=lambda p: (-len(set(parts) & set(PurePosixPath(p).parts[:-1])), p))
        result.append([source, *matches[:2]])
    return result


def read_file(base, sha, path, maximum=MAX_CHARACTERS):
    if not safe_path(path):
        raise ValueError('Invalid discovery path')
    size = int(local_git(base, 'cat-file', '-s', sha + ':' + path).strip())
    if not 0 < size <= maximum:
        raise ValueError('Discovery file exceeds bounded complete-read size: ' + path)
    value = local_git(base, 'show', sha + ':' + path)
    if '\0' in value or len(value) > maximum:
        raise ValueError('Discovery file is binary or too large: ' + path)
    return value


def policy_references(path, body, available):
    """Resolve explicit normative references; ordinary navigation is not policy."""
    references = set()
    def resolve(href):
        href = href.split('#', 1)[0].split('?', 1)[0]
        if not href:
            return
        if '://' in href or href.startswith('//'):
            if 'docs.github.com/' in href:
                return  # General GitHub usage help, not repository-specific rules.
            raise ValueError('External policy reference requires separate complete collection')
        href = unquote(href)
        relative = os.path.normpath(str(PurePosixPath(path).parent / href)).replace('\\', '/')
        candidate = next((p for p in (relative, href) if safe_path(p) and p in available), None)
        if candidate is None:
            raise ValueError('Unresolved local contribution policy reference: ' + href)
        references.add(candidate)

    for paragraph in re.split(r'\n\s*\n', body):
        normative = bool(re.search(r'\b(read|follow|required?|must|shall|requirements|mandatory)\b', paragraph, re.I))
        for match in re.finditer(r'\[([^\]]+)\]\(([^)]+)\)', paragraph):
            label, href = match.groups()
            # A required template and a support-chat link can share a paragraph;
            # only the sentence directing the reader grants a normative relation.
            prefix = paragraph[:match.start()]
            context = (prefix.rsplit('\n', 1)[-1] if PurePosixPath(path).suffix.lower() in ('.yml', '.yaml')
                       else re.split(r'(?<=[.!?])\s+', prefix)[-1])
            directed = re.search(r'\b(read|follow|required?|must|shall|requirements|mandatory)\b', context, re.I)
            if directed or re.search(r'contribut|guideline|polic|agent.*rule|style.?guide|requirements|template', label + ' ' + href, re.I):
                resolve(href)
        if not normative:
            continue
        for match in re.finditer(r'`([^`\n]+)`', paragraph):
            value = match.group(1)
            if not re.search(r'\.(md|mdx|rst|txt|ya?ml)$', value, re.I):
                continue
            relative = os.path.normpath(str(PurePosixPath(path).parent / value)).replace('\\', '/')
            explicitly_read = re.search(r'\b(read|follow|consult|refer to|according to|defined in)\b[^`]{0,80}$',
                                        paragraph[:match.start()], re.I)
            if value in available or relative in available or explicitly_read:
                resolve(value)
        if re.search(r'\bbug\b.*\b(issue\s+)?templates?\b', paragraph, re.I | re.S):
            bugs = {p for p in available if p.casefold().startswith('.github/issue_template/')
                    and re.search(r'bug', PurePosixPath(p).name, re.I)
                    and PurePosixPath(p).suffix.casefold() in ('.md', '.yml', '.yaml')}
            if not bugs:
                raise ValueError('Required bug issue template is unavailable')
            references.update(bugs)
            references.update(p for p in available if p.casefold() in
                              ('.github/issue_template/config.yml', '.github/issue_template/config.yaml'))
        if re.search(r'\b(pr|pull.request)\s+templates?\b', paragraph, re.I):
            templates = {p for p in available if p.casefold() in ('.github/pull_request_template.md', 'pull_request_template.md')}
            if not templates:
                raise ValueError('Required PR template is unavailable')
            references.update(templates)
    return references


def collect_scope(pipeline, record, after='', progress=None):
    """Collect one complete small scope. Missing cached material stays unknown."""
    repo = record['repo']
    base = pipeline.root / 'workspaces' / repo.replace('/', '--')
    if not (base / '.git').exists():
        raise ValueError('No existing base clone; source discovery does not clone')
    origin = local_git(base, 'remote', 'get-url', 'origin').strip()
    if origin.rstrip('/').removesuffix('.git').casefold() != ('https://github.com/' + repo).casefold():
        raise ValueError('Base clone origin does not match verified repository')
    branch = record.get('default_branch', '')
    if not branch or branch.startswith('-') or any(c in branch for c in ('\n', '\r', ':')):
        raise ValueError('Missing valid catalog default branch')
    sha = local_git(base, 'rev-parse', '--verify', 'refs/remotes/origin/' + branch + '^{commit}').strip()
    if not re.fullmatch('[a-f0-9]{40}', sha):
        raise ValueError('Discovery requires an immutable local tracking SHA')
    entries = local_git(base, 'ls-tree', '-r', '-l', '-z', sha).split('\0')
    if len(entries) > MAX_PATHS + 1:
        raise ValueError('Cached source inventory exceeds discovery bound')
    sizes = {}
    for entry in filter(None, entries):
        metadata, path = entry.split('\t', 1)
        mode, kind, _, size = metadata.split()
        if mode in ('100644', '100755') and kind == 'blob' and size.isdigit() and safe_path(path):
            sizes[path] = int(size)
    paths = list(sizes)
    options = scope_options([p for p in paths if 0 < sizes[p] <= MAX_CHARACTERS])
    if not options:
        raise ValueError('No bounded source module with matching tracked tests')
    selected = next((p for p in options if p[0] > after), options[0])
    if progress is not None:
        progress.update(module=selected[0], sha=sha)
    available = set(paths)
    rules = {p for p in paths if p.casefold() in {
        'agents.md', 'claude.md', 'contributing.md', 'security.md', 'ai_policy.md',
        '.github/contributing.md', '.github/security.md', '.github/ai_policy.md',
        'docs/contributing.md', 'docs/security.md'}}
    rules.update(p for p in paths if PurePosixPath(p).name.casefold() in ('agents.md', 'claude.md', 'contributing.md') and
                 any(s.startswith(p.rsplit('/', 1)[0] + '/') for s in selected))
    guide = record.get('contributing_url') or ''
    if guide:
        prefix = 'repos/' + repo + '/contents/'
        if prefix not in guide:
            raise ValueError('Shared/external contribution rules require separate complete collection')
        path = unquote(guide.split(prefix, 1)[1].split('?', 1)[0])
        if path not in available:
            raise ValueError('Catalog contribution guide is absent at selected SHA')
        rules.add(path)
    if record.get('shared_policy') or not rules:
        raise ValueError('Complete applicable local policy is unavailable')
    policy = {}
    while rules - policy.keys():
        if len(rules) > MAX_RULES:
            raise ValueError('Applicable discovery rules exceed complete-read bound')
        path = sorted(rules - policy.keys())[0]
        policy[path] = read_file(base, sha, path, MAX_POLICY_CHARACTERS)
        rules.update(policy_references(path, policy[path], available))
    sources = {path: read_file(base, sha, path) for path in selected[:MAX_FILES]}
    manifest = {'version': 1, 'repo': repo, 'sha': sha, 'source_paths': list(sources),
                'policy_paths': sorted(policy), 'source_digest': digest(sources),
                'policy_digest': digest(policy), 'observed_at': now(),
                'freshness': 'existing local origin tracking ref; not a live default-branch refresh',
                'selection': 'tracked source module and matching tests; no upstream issue seed'}
    manifest['scope_id'] = digest({key: manifest[key] for key in
                                  ('repo', 'source_paths', 'policy_paths', 'source_digest', 'policy_digest')})
    packet = {'schema_version': 2, 'mode': 'source_discovery', 'at': now(), 'complete': True,
              'discovery_manifest': manifest, 'policy': policy, 'peers': [], 'selected_peers': [],
              'target': {'repo': repo, 'sha': sha, 'files': {**policy, **sources},
                         'checked_paths': [*sorted(policy), *sources],
                         'selection': {'focus': 'Inspect local behavior in ' + selected[0],
                                       'preferred_paths': list(sources), 'basis': manifest['selection']}},
              'network_requests': 0, 'recent_issues_and_prs': [], 'recent_releases': [],
              'peer_selection': 'Local defect investigation; no cross-project feature claims'}
    packet['evidence_sources'] = evidence_catalog(packet)
    from research import limits
    if len(json.dumps(packet, ensure_ascii=False)) > limits(pipeline)['context_characters']:
        raise ValueError('Complete source and policy packet exceeds research context bound')
    return packet


def load_packet(pipeline, task):
    payload = json.loads(task['payload']) if isinstance(task['payload'], str) else task['payload']
    saved = payload.get('source_discovery')
    if not isinstance(saved, dict):
        raise ValueError('Task is not a source discovery scope')
    packet = json.loads((pipeline.root / 'runs' / task['id'] / 'source-discovery.json').read_text(encoding='utf-8'))
    manifest = packet.get('discovery_manifest', {})
    if (packet.get('mode') != 'source_discovery' or packet.get('complete') is not True
            or manifest != saved or manifest.get('repo') != task['repo']
            or manifest.get('sha') != task['base_sha'] or packet.get('peers') != []
            or packet.get('selected_peers') != []):
        raise ValueError('Discovery manifest does not match registered task')
    source_paths, policy_paths = manifest['source_paths'], manifest['policy_paths']
    files = packet['target']['files']
    if (not isinstance(source_paths, list) or not isinstance(policy_paths, list)
            or not re.fullmatch('[a-f0-9]{40}', manifest.get('sha', ''))
            or not 2 <= len(source_paths) <= MAX_FILES or not 1 <= len(policy_paths) <= MAX_RULES
            or len(set(source_paths)) != len(source_paths) or len(set(policy_paths)) != len(policy_paths)
            or set(source_paths) & set(policy_paths)
            or set(files) != set(source_paths) | set(policy_paths)
            or any(not safe_path(path) for path in files)
            or packet['target']['repo'] != task['repo'] or packet['target']['sha'] != task['base_sha']
            or packet['target']['checked_paths'] != [*sorted(policy_paths), *source_paths]
            or any(not isinstance(value, str) or not value or '\0' in value or len(value) >
                   (MAX_POLICY_CHARACTERS if path in policy_paths else MAX_CHARACTERS) for path, value in files.items())
            or digest({p: files[p] for p in source_paths}) != manifest['source_digest']
            or digest({p: files[p] for p in policy_paths}) != manifest['policy_digest']
            or packet['policy'] != {p: files[p] for p in policy_paths}
            or manifest['scope_id'] != digest({key: manifest[key] for key in
                ('repo', 'source_paths', 'policy_paths', 'source_digest', 'policy_digest')})):
        raise ValueError('Discovery scope content or policy digest mismatch')
    packet['evidence_sources'] = evidence_catalog(packet)
    packet['task'] = dict(task)
    return packet


def pinned_scope(pipeline, task):
    return load_packet(pipeline, task)['target']['sha']


def register_scopes(engine, limit):
    """Return up to two pending/new scopes for the existing research quota."""
    if type(limit) is not int or not 0 <= limit <= 2:
        raise ValueError('Source discovery limit must be 0..2')
    if limit == 0:
        return []
    from repository_catalog import research_pool
    from failures import retry_ready
    p = engine.p
    catalog_path = p.root / p.config.get('repository_catalog', 'repository-catalog.json')
    if not catalog_path.exists():
        return []  # This lane requires retained verified catalog records.
    pool = research_pool(p)
    records = {r['repo']: r for r in json.loads(catalog_path.read_text(encoding='utf-8'))['repositories']}
    pending = p.db.execute("SELECT id,repo FROM tasks WHERE source='hunt' AND state IN ('DISCOVERED','PLAN') "
                           "AND json_type(payload,'$.source_discovery')='object' ORDER BY updated,id").fetchall()
    preferred, selected_repos = [], set()
    for row in p.fair_candidates(pending, repositories=pool, cursor_name='source-discovery'):
        data = engine.load(row['id'])
        if (row['repo'] not in selected_repos and data['stage'] == 'PLAN' and not data.get('research', {}).get('approved')
                and data['attempts'].get('RESEARCH', 0) < engine.options.get('max_stage_attempts', 3)
                and retry_ready(data, 'RESEARCH')):
            preferred.append(row['id'])
            selected_repos.add(row['repo'])
            with p.db:
                p.db.execute("INSERT INTO scheduler VALUES ('source-discovery',?) ON CONFLICT(name) DO UPDATE SET last_repo=excluded.last_repo", (row['repo'],))
            if len(preferred) == limit:
                return preferred
    rows = [{'id': repo, 'repo': repo} for repo in pool if repo in records and repo not in selected_repos]
    report = []
    for row in p.fair_candidates(rows, repositories=pool, cursor_name='source-discovery')[:8]:
        if len(preferred) >= limit:
            break
        repo = row['repo']
        cursor_name = 'source-discovery:' + repo
        previous = p.db.execute('SELECT last_repo FROM scheduler WHERE name=?', (cursor_name,)).fetchone()
        progress = {}
        try:
            packet = collect_scope(p, records[repo], previous[0] if previous else '', progress=progress)
            manifest = packet['discovery_manifest']
            key = repo.replace('/', '--') + '--hunt-source-' + manifest['scope_id'][:24]
            if not p.db.execute('SELECT 1 FROM tasks WHERE id=?', (key,)).fetchone():
                focus = 'Inspect independently reproducible local behavior in ' + manifest['source_paths'][0]
                # Complete immutable source/policy evidence exists before task registration.
                save(p.root / 'runs' / key / 'source-discovery.json', packet)
                with p.db:
                    p.db.execute('INSERT INTO tasks(id,repo,source,title,score,state,holds,payload,updated,base_sha) '
                                 'VALUES (?,?,?,?,?,?,?,?,?,?)',
                                 (key, repo, 'hunt', focus, 60, 'DISCOVERED', '[]',
                                  json.dumps({'focus': focus, 'source_discovery': manifest}), now(), manifest['sha']))
                preferred.append(key)
                status = 'registered'
            else:
                status = 'unchanged_scope_reused'
            report.append({'repo': repo, 'status': status, 'task': key, 'sha': manifest['sha']})
        except BudgetExhausted:
            raise
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired, KeyError) as error:
            report.append({'repo': repo, 'status': 'unknown', 'reason': str(error), **progress})
        finally:
            with p.db:
                # A failed nested rule or missing cached blob is unknown, not a
                # finding. Yield the module turn without creating/resetting a task.
                if progress.get('module'):
                    p.db.execute('INSERT INTO scheduler VALUES (?,?) ON CONFLICT(name) DO UPDATE SET last_repo=excluded.last_repo',
                                 (cursor_name, progress['module']))
                p.db.execute("INSERT INTO scheduler VALUES ('source-discovery',?) ON CONFLICT(name) DO UPDATE SET last_repo=excluded.last_repo", (repo,))
    save(p.root / 'runs/source-discovery-summary.json', {'at': now(), 'repositories': report,
                                                      'preferred_tasks': preferred, 'limit': limit})
    return preferred
