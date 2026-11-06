"""Bounded, read-only opportunity research, using the existing model and gates."""
import json
import hashlib
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from contribute import command, execute, gh, now, save
from model_selection import research_model
from telemetry import BudgetExhausted, TaskBudgetExhausted


CATEGORIES = ('feature', 'performance', 'security', 'ux', 'reliability', 'bug', 'tests')
FIELDS = ('title', 'background', 'users', 'expected_effect', 'scope', 'validation',
          'risks', 'effort', 'acceptance', 'comparison', 'issue_body')


def limits(pipeline):
    options = pipeline.config.get('research', {})
    bounds = {'peers_per_candidate': (3, 3, 5), 'files_per_peer': (4, 2, 6),
              'target_files': (12, 3, 25), 'file_characters': (12000, 100, 60000),
              'context_characters': (320000, 1000, 600000), 'network_requests': (40, 1, 60)}
    result = {}
    for key, (default, low, high) in bounds.items():
        value = options.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError('Research limit out of range: ' + key)
        result[key] = value
    return result


def focus_terms(focus):
    words = set(re.findall(r'[a-z0-9_]+', focus.lower())) - {
        'the', 'and', 'one', 'with', 'from', 'research', 'compare', 'without',
        'losing', 'completed', 'repeating', 'interrupted', 'runs', 'actions',
        'assess', 'minimal', 'optional', 'upstream', 'demand', 'design'}
    concepts = [
        {'session', 'sessions', 'recovery', 'resume', 'checkpoint', 'checkpoints',
         'conversation', 'history', 'restore', 'trajectory', 'persist', 'agent', 'serialize'},
        {'context', 'compact', 'compress', 'message'},
    ]
    for related in concepts:
        if words & related:
            words.update(related)
    return sorted(w for w in words if len(w) > 3)


def select_paths(paths, focus, count, content_matches=(), preferred_paths=()):
    from feature_matrix import file_type
    words = focus_terms(focus)
    matches = set(content_matches)
    preferred = set(preferred_paths)
    def relevance(path):
        runtime = bool(re.search(r'(^|/)(src|lib|core|agents?)/', path.lower()))
        housekeeping = path.startswith(('.github/', 'scripts/', 'docs/', 'examples/'))
        helper = Path(path).name in ('__init__.py', '__main__.py', 'conftest.py')
        tokens = re.findall(r'[a-z0-9]+', re.sub(r'([a-z])([A-Z])', r'\1_\2', path).lower())
        score = sum(any(t.startswith(w) for t in tokens) for w in words) + 3 * (path in matches)
        return (path not in preferred, helper, -score, housekeeping, not runtime, len(path), path)
    ordered = sorted((p for p in paths if file_type(p) != 'configuration'), key=relevance)
    selected = []
    # Reserve coverage for executable evidence and documentation, then fill by relevance.
    for kind in ('source', 'test', 'documentation'):
        match = next((p for p in ordered if file_type(p) == kind), None)
        if match and len(selected) < count:
            selected.append(match)
    return (selected + [p for p in ordered if p not in selected])[:count]


def demand_selection(task, recent, paths):
    """Use issue text only as bounded search hints, never evidence or instructions."""
    title = task['title']
    original = {'focus': title, 'preferred_paths': [], 'basis': 'task focus'}
    if (task.get('source') != 'hunt' or
            not title.startswith('Prioritize valuable features across multiple harnesses') or
            not re.search(r'Research batch \d{4}-W\d{2}', title)):
        return original
    from feature_matrix import file_type
    eligible = {path for path in paths if file_type(path) in ('source', 'test')}
    candidates = []
    for item in recent[:20]:
        if item.get('state') != 'open' or item.get('is_pull_request') or 'pull_request' in item:
            continue
        issue_title = str(item.get('title') or '').strip()[:300]
        if not issue_title or not str(item.get('number', '')).isdigit():
            continue
        text = issue_title + '\n' + str(item.get('body') or '')[:2000]
        references = []
        for token in re.findall(r'[A-Za-z0-9_./@+\[\]-]+', text):
            path = token.removeprefix('./')
            if path in eligible and path not in references:
                references.append(path)
        candidates.append({'focus': issue_title, 'preferred_paths': references[:8],
                           'issue_number': str(item['number']),
                           'basis': 'untrusted recent open issue discovery hints; not verified demand or approval'})
    # Keep API recency order within each group; explicit baseline paths give a
    # firmer scope than a title alone. Later duplicate/policy gates still apply.
    return next((item for item in candidates if item['preferred_paths']),
                candidates[0] if candidates else original)


def search_target(work, sha, focus, paths):
    """Search tracked baseline text, never execute target code or inspect mutable files."""
    terms = [w for w in focus_terms(focus) if w != 'agent']
    if not terms:
        return []
    result = execute(['git', 'grep', '-l', '-I', '-i', '-E',
                      '|'.join(re.escape(w) for w in terms), sha, '--'], cwd=work)
    if result.returncode not in (0, 1):
        raise RuntimeError('Baseline content search failed: ' + result.stderr[-1000:])
    allowed = set(paths)
    return sorted({line[len(sha) + 1:] for line in result.stdout.splitlines()
                   if line.startswith(sha + ':') and line[len(sha) + 1:] in allowed})


def bounded_search_inventory(search):
    """Bound diagnostic paths, never source excerpts or the selection input."""
    matches = search.get('matched_paths', [])
    selected, size = [], 0
    for path in matches:
        encoded_size = len(json.dumps(path, ensure_ascii=False)) + 2
        if len(selected) >= 100 or size + encoded_size > 12000:
            break
        selected.append(path)
        size += encoded_size
    count = search.get('matched_count', len(matches))
    return {**search, 'matched_paths': selected, 'matched_count': count,
            'matched_paths_truncated': bool(search.get('matched_paths_truncated') or len(selected) < len(matches))}


def select_peers(pipeline, task, pool, count):
    """Use verified categories and bounded inventories for relevance, rotate equal matches."""
    catalog_file = pipeline.root / pipeline.config.get('repository_catalog', 'repository-catalog.json')
    records = {}
    if catalog_file.exists():
        records = {r['repo']: r for r in json.loads(catalog_file.read_text(encoding='utf-8'))['repositories']}
    target_category = records.get(task['repo'], {}).get('category', 'coding_harness')
    words = {w for w in task['title'].lower().replace('-', ' ').split() if len(w) > 3}
    def order(repo):
        record = records.get(repo, {})
        inventory = ' '.join(p for v in record.get('inventory', {}).values() for p in v.get('examples', []))
        relevance = sum(word in (repo + ' ' + inventory).lower() for word in words)
        relevance += 3 * (record.get('category', 'coding_harness') == target_category)
        rotation = hashlib.sha256((task['id'] + repo).encode()).hexdigest()
        return -relevance, rotation
    return sorted((r for r in dict.fromkeys(pool) if r.lower() != task['repo'].lower()), key=order)[:count]


def snapshot(repo, focus='', options=None, request=None, raw=None):
    """Pin every read, and bound the inventory, file count and text supplied to models."""
    options = options or {'files_per_peer': 4, 'file_characters': 12000}
    request, raw = request or gh, raw or command
    meta = request('api', f'repos/{repo}')
    if meta.get('archived') or meta.get('disabled') or meta.get('private'):
        raise ValueError('Peer is no longer eligible for research')
    if meta.get('full_name', repo).lower() != repo.lower():
        raise ValueError('Peer moved; catalog re-verification required')
    sha = request('api', f"repos/{repo}/commits/{meta['default_branch']}")['sha']
    if not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{40}', sha):
        raise ValueError('Peer commit lookup did not return an immutable SHA')
    tree = request('api', f'repos/{repo}/git/trees/{sha}?recursive=1')
    if tree.get('truncated'):
        raise ValueError('Research source tree is truncated: ' + repo)
    paths = [x['path'] for x in tree['tree'] if x['type'] == 'blob' and x.get('size', 60001) <= 60000]
    files = {}
    error = None
    for path in select_paths(paths, focus, options['files_per_peer']):
        try:
            files[path] = raw(['gh', 'api', f"repos/{repo}/contents/{quote(path, safe='/')}?ref={sha}",
                               '-H', 'Accept: application/vnd.github.raw+json'])[:options['file_characters']]
        except BudgetExhausted:
            raise
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as failure:
            error = type(failure).__name__
            break
    return {'repo': repo, 'sha': sha, 'files': files, 'checked_paths': list(files),
            'inventory_count': len(paths), 'error': error, 'evidence_scope': 'bounded excerpts; absence is scope limited'}


def collect(engine, task):
    from repository_catalog import research_pool
    from telemetry import ACTIVE, BudgetExhausted
    p, options = engine.p, limits(engine.p)
    checkpoint = engine.packet_dir(task['id']) / 'research-context.json'
    payload = json.loads(task['payload']) if isinstance(task.get('payload'), str) else task.get('payload', {})
    if payload.get('source_discovery'):
        from source_discovery import load_packet
        packet = load_packet(p, task)
        save(checkpoint, packet)
        if len(json.dumps(packet, ensure_ascii=False)) > options['context_characters']:
            raise TaskBudgetExhausted('research_context_characters_exhausted')
        current = ACTIVE.get()
        if current:
            current.count('researched_repositories')
            from feature_matrix import file_type
            for path in packet['target']['files']:
                current.count('files_' + file_type(path))
        return packet
    packet = {}
    if checkpoint.exists():
        previous = json.loads(checkpoint.read_text(encoding='utf-8'))
        if previous.get('schema_version') == 2 and previous.get('task', {}).get('base_sha') == task['base_sha']:
            packet = previous
    if not packet:
        peers = select_peers(p, task, research_pool(p), options['peers_per_candidate'])
        packet = {'schema_version': 2, 'at': now(), 'task': task, 'policy': {}, 'peers': [],
                  'selected_peers': peers, 'network_requests': 0, 'complete': False,
                  'peer_selection': 'Verified category and bounded path relevance; deterministic rotation of ties'}
    packet['complete'] = False
    requests = 0
    def budget():
        nonlocal requests
        if requests >= options['network_requests']:
            raise TaskBudgetExhausted('research_network_requests_exhausted')
        requests += 1
        packet['network_requests'] += 1
        current = ACTIVE.get()
        if current:
            current.remaining()
            current.count('research_network_requests')
    def request(*args):
        budget()
        return gh(*args)
    def raw(*args, **kwargs):
        budget()
        return command(*args, **kwargs)
    def persist():
        save(checkpoint, packet)
    try:
        if 'recent_issues_and_prs' not in packet:
            packet['recent_issues_and_prs'] = [
                {**{k: str(item.get(k) or '')[:2000] for k in ('number', 'title', 'body', 'html_url', 'state')},
                 'is_pull_request': 'pull_request' in item}
                for item in request('api', f"repos/{task['repo']}/issues?state=all&sort=updated&per_page=20")]
            persist()
        if 'target' not in packet:
            work = Path(task['workspace'])
            # Read tracked content at the recorded baseline, never mutable/untracked files.
            inventory = command(['git', 'ls-tree', '-r', '-l', task['base_sha']], cwd=work)
            paths = []
            for line in inventory.splitlines():
                metadata, path = line.split('\t', 1)
                fields = metadata.split()
                if fields[1] == 'blob' and fields[3].isdigit() and int(fields[3]) <= 60000:
                    paths.append(path)
            policy_names = [p.contributing_path(task['repo']), 'CONTRIBUTING.md', 'docs/contributing.md',
                            '.github/CONTRIBUTING.md', 'docs/CONTRIBUTING.md', '.github/contributing.md', 'AGENTS.md',
                            'SECURITY.md', '.github/SECURITY.md', 'docs/SECURITY.md']
            selection = demand_selection(task, packet['recent_issues_and_prs'], paths)
            focus, preferred = selection['focus'], selection['preferred_paths']
            # Include nested rules applying to selected code paths, within the target read cap.
            matches = search_target(work, task['base_sha'], focus, paths)
            selected = select_paths(paths, focus, options['target_files'], matches, preferred)
            policy_names += [x for x in paths if x.endswith('/AGENTS.md') and
                             any(y.startswith(x.rsplit('/', 1)[0] + '/') for y in selected)]
            policy_names = list(dict.fromkeys(x for x in policy_names if x in paths))
            while True:
                if len(policy_names) >= options['target_files']:
                    raise TaskBudgetExhausted('target_policy_file_budget_exhausted')
                selected = policy_names + select_paths([x for x in paths if x not in policy_names],
                    focus, options['target_files'] - len(policy_names), matches, preferred)
                nested = [x for x in paths if x.endswith('/AGENTS.md') and x not in policy_names
                          and any(y.startswith(x.rsplit('/', 1)[0] + '/') for y in selected)]
                if not nested:
                    break
                policy_names.extend(nested)
            files = {path: command(['git', 'show', task['base_sha'] + ':' + path], cwd=work)[:options['file_characters']]
                     for path in selected}
            packet['policy'] = {path: files[path] for path in policy_names}
            packet['target'] = {'repo': task['repo'], 'sha': task['base_sha'], 'files': files, 'checked_paths': selected,
                                'selection': selection,
                                'content_search': {'terms': focus_terms(focus), 'matched_paths': matches,
                                                   'scope': 'eligible tracked baseline text; matches are not absence proof'}}
        # Also repair older checkpoints whose diagnostic inventory exceeded the
        # context cap. Preserve all pinned source evidence and peer identities.
        if 'content_search' in packet['target']:
            packet['target']['content_search'] = bounded_search_inventory(packet['target']['content_search'])
            persist()
        for peer in packet['selected_peers']:
            if any(s['repo'] == peer and not s.get('error') for s in packet['peers']):
                continue
            try:
                source = snapshot(peer, packet['target'].get('selection', {}).get('focus', task['title']),
                                  options, request, raw)
            except BudgetExhausted:
                raise
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired, KeyError, TypeError) as error:
                source = {'repo': peer, 'sha': None, 'files': {}, 'error': type(error).__name__, 'status': 'unknown'}
            packet['peers'] = [s for s in packet['peers'] if s['repo'] != peer] + [source]
            persist()
        if 'recent_releases' not in packet:
            packet['recent_releases'] = [
                {k: str(item.get(k) or '')[:2000] for k in ('tag_name', 'body', 'html_url')}
                for item in request('api', f"repos/{task['repo']}/releases?per_page=3")]
        from feature_matrix import evidence_catalog
        packet['evidence_sources'] = evidence_catalog(packet)
        if len(json.dumps(packet, ensure_ascii=False)) > options['context_characters']:
            raise TaskBudgetExhausted('research_context_characters_exhausted')
        packet['complete'] = True
        current = ACTIVE.get()
        if current:
            current.count('selected_peers', len(packet['peers']))
            current.count('researched_repositories', 1 + len(packet['peers']))
            from feature_matrix import file_type
            for source in [packet['target'], *packet['peers']]:
                for path in source['files']:
                    current.count('files_' + file_type(path))
        return packet
    finally:
        persist()


def validate(answer, task, packet):
    if answer.get('decision') not in ('candidate', 'none', 'private'):
        raise ValueError('Invalid research decision')
    if not isinstance(answer.get('reason'), str) or not answer['reason'].strip():
        raise ValueError('Research needs a reason')
    if answer['decision'] != 'candidate':
        return
    if answer.get('category') not in CATEGORIES or answer.get('risk') not in ('low', 'medium', 'high'):
        raise ValueError('Invalid opportunity category/risk')
    for field in FIELDS:
        if not isinstance(answer.get(field), str) or not answer[field].strip():
            raise ValueError('Missing opportunity field: ' + field)
    queries = answer.get('duplicate_queries')
    if not isinstance(queries, list) or not queries or len(queries) > 2 or any(not isinstance(x, str) or not x.strip() for x in queries):
        raise ValueError('One or two specific duplicate queries required')
    policy = answer.get('policy')
    if not isinstance(policy, dict) or any(type(policy.get(k)) is not bool for k in
            ('automated_issue', 'automated_pr', 'maintainer_required')) or not policy.get('reason'):
        raise ValueError('Explicit upstream policy assessment required')
    # Resolve controller references before exact checks; supplied/stale IDs grant no trust.
    from feature_matrix import evidence_catalog, resolve_evidence
    catalog = evidence_catalog(packet)
    # Quotes are checked against actual pinned files, not model-provided URLs.
    for group in ('evidence', 'policy_evidence'):
        evidence = answer.get(group)
        if not isinstance(evidence, list) or not evidence:
            raise ValueError('Missing research evidence: ' + group)
        for index, item in enumerate(evidence):
            item = resolve_evidence(item, catalog)
            evidence[index] = item
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k].strip() for k in ('repo', 'path', 'quote')):
                raise ValueError('Invalid source evidence')
            if item['repo'] == task['repo']:
                work = Path(task['workspace']).resolve()
                file = (work / item['path']).resolve()
                text = (packet['target']['files'].get(item['path'], '') if packet.get('schema_version') == 2 else
                        file.read_text(encoding='utf-8') if file.is_relative_to(work) and file.is_file() else '')
                sha = task['base_sha']
                if group == 'policy_evidence' and item['path'] not in packet['policy']:
                    raise ValueError('Policy evidence must cite applicable target rules')
            else:
                if group == 'policy_evidence':
                    raise ValueError('Policy evidence must cite target rules, not a peer')
                peer = next((x for x in packet['peers'] if x['repo'] == item['repo']), {})
                text = peer.get('files', {}).get(item['path'], '')
                sha = peer.get('sha')
            if item['quote'] not in text or not text:
                raise ValueError('Research quote does not match source: ' + item['path'])
            if item.get('sha', sha) != sha:
                raise ValueError('Research evidence SHA mismatch')
            item['sha'] = sha
            from feature_matrix import file_type
            item['file_type'] = file_type(item['path'])
            item['url'] = f"https://github.com/{item['repo']}/blob/{sha}/{quote(item['path'], safe='/')}"
    if not any(x['repo'] == task['repo'] for x in answer['evidence']):
        raise ValueError('Target implementation evidence required')
    if (answer['category'] == 'feature' and packet.get('mode') != 'source_discovery'
            and not any(x['repo'] != task['repo'] for x in answer['evidence'])):
        raise ValueError('Feature comparisons require actual peer implementation evidence')


def correction_feedback(answer, packet, error):
    """Report all mechanical evidence errors together, so one repair can be useful."""
    sources = {s['repo']: s for s in [packet.get('target', {}), *packet['peers']] if s.get('repo')}
    errors = [str(error)]
    matrix = answer.get('feature_matrix', {})
    if not isinstance(matrix, dict):
        matrix = {}
    for field in FIELDS:
        if not isinstance(answer.get(field), str) or not answer[field].strip():
            errors.append('Top-level ' + field + ' must be a nonempty string')
    for field in ('feature_name', 'user_scenario', 'gap_analysis', 'migration_analysis', 'risks'):
        if not isinstance(matrix.get(field), str) or not matrix[field].strip():
            errors.append('feature_matrix.' + field + ' must be a nonempty string')
    for field in ('affected_files', 'implementation_plan', 'validation_plan'):
        items = matrix.get(field)
        if not isinstance(items, list) or not items or any(not isinstance(x, str) for x in items):
            errors.append('feature_matrix.' + field + ' must be a nonempty array of strings')
    statuses = matrix.get('feature_status', answer.get('feature_status', {}))
    claims = [x for group in ('evidence', 'policy_evidence') if isinstance(answer.get(group), list)
              for x in answer[group] if isinstance(x, dict)]
    if isinstance(statuses, dict):
        for repo, status in statuses.items():
            if not isinstance(status, dict):
                continue
            claims.extend(x for x in (status.get('evidence') or []) if isinstance(x, dict))
            for path in (status.get('checked_paths') or []):
                if not isinstance(path, str) or path not in sources.get(repo, {}).get('files', {}):
                    errors.append('Unread checked_path for ' + repo + ': ' + str(path))
    from feature_matrix import evidence_catalog, resolve_evidence
    catalog = evidence_catalog(packet)
    for claim in claims:
        try:
            claim = resolve_evidence(claim, catalog)
        except ValueError as failure:
            errors.append(str(failure))
            continue
        source = sources.get(claim.get('repo'), {}).get('files', {})
        path, excerpt = claim.get('path'), claim.get('quote')
        if not isinstance(path, str) or not isinstance(excerpt, str) or not excerpt or excerpt not in source.get(path, ''):
            errors.append('Non-exact or unread quote: ' + str(claim))
    return '\n'.join(dict.fromkeys(errors))[:8000]


def run(engine, task_id, data):
    """No public side effects. Persist the result and let PLAN consume it later."""
    p = engine.p
    p.require_research(p.task(task_id))
    p.prepare(task_id, read_only=True)
    task = p.task(task_id)
    from contribution_strategy import local_discovery, PRIORITY_GUIDANCE
    if local_discovery(task, data) and not data.get('contribution_origin'):
        data['contribution_origin'] = {'kind': 'local_discovery', 'task': task_id, 'at': now()}
        payload = json.loads(task['payload']) if isinstance(task.get('payload'), str) else task.get('payload', {})
        if payload.get('source_discovery'):
            scope = payload['source_discovery']
            data['contribution_origin'].update(scope_id=scope['scope_id'], sha=scope['sha'],
                                               source_paths=scope['source_paths'])
        engine.store(task_id, data)
    proactive = engine.proactive(task_id, data)
    before = p.fingerprint(task['workspace'])
    packet = collect(engine, task)
    from research_scope import resolution_digest, scope_identity
    resolved_digest = resolution_digest(data)
    resolved_scope = data.get('scope_overlap_review') if resolved_digest else None
    prior_matrix = engine.db.execute('SELECT matrix FROM research_candidates WHERE task=? ORDER BY updated_at DESC LIMIT 1',
                                     (task_id,)).fetchone() if resolved_scope else None
    resolved_feature_key = json.loads(prior_matrix['matrix']).get('feature_key') if prior_matrix else None
    if resolved_scope:
        packet = {**packet, 'scope_overlap_review': resolved_scope,
                  'scope_overlap_history': data.get('scope_overlap_history', [])[-1:]}
    from feature_matrix import PROMPT, build, persist
    from telemetry import ACTIVE
    prompt = ('Research ONE valuable opportunity, or return none. Prefer explicit user demand, bounded scope, '
        'independently reproducible failures and affordable verification under the current architecture and policy. '
        'Retain valuable feature ideas, but unconfirmed direction must wait separately from deliverable fixes. '
        'Read actual target code, docs, tests, '
        'designs and changelogs and compare supplied official peer evidence. Cover features, performance, '
        'security, UX/accessibility and reliability as relevant to the focus. A seemingly missing feature is '
        'not evidence: assess the supplied implementations, tests and design docs; consider project scope. '
        'You cannot inspect unsupplied files. Never claim repository-wide absence or searches you did not perform. '
        'Inspect the supplied user feedback, issues and PRs; the controller will also search open AND closed '
        'duplicates. Never invent execution or negative search evidence. No edits, shell, publishing or delegation. '
        'For exploitable security findings return decision=private with a non-sensitive reason; never include '
        'exploit details in title/search queries/public body. Follow SECURITY.md and private disclosure policy. '
        'Return ONLY JSON with decision=candidate|none|private, reason. For candidate include category='
        'feature|performance|security|ux|reliability|bug|tests, risk=low|medium|high, '
        + ', '.join(FIELDS) + ' (EVERY one of these fields must be a nonempty STRING, not an array/object), duplicate_queries (1-2 precise searches), '
        'evidence and policy_evidence arrays of {evidence_id,quote}, using controller evidence_sources IDs '
        'and EXACT source excerpts. Legacy {repo,path,quote} is accepted only for actually supplied files. '
        'Prefer short contiguous quotes under 200 characters; never join separated lines or paraphrase a quote. '
        'policy={automated_issue:bool,automated_pr:bool,maintainer_required:bool,reason:string}. '
        'automated_pr describes whether upstream POLICY permits automated implementation/PRs once all '
        'required maintainer approvals exist; use maintainer_required for missing design approval. '
        'An explicit automation ban or unverifiable policy means automated_pr=false, which approval cannot override. '
        'Describe cost in effort. acceptance means maintainer acceptance LIKELIHOOD with evidence and rationale, '
        'not test acceptance criteria. State who requested this or which contribution priority it fits; '
        'consider validation cost, architecture fit, candidate age, actual maintainer responses and prior '
        'external contribution acceptance when supplied; label missing observations unknown. '
        'do not invent maintainer approval. Keep test acceptance criteria in validation. Include concrete validation commands, '
        'expected result and affected files in scope. Policy evidence must quote applicable contribution rules. '
        + ('Features and bounded fixes may proceed without discretionary maintainer encouragement when upstream '
           'rules permit. Architecture changes, uncertain designs and high risks need direction. '
           if proactive else 'Features, architecture changes, uncertain designs and non-low risks always need '
           'maintainer direction before any implementation. ')
        + 'Small proven bugs/tests/performance/UX can proceed only if policy permits. '
        'issue_body must be English, accurate about static evidence and pending tests. Be concise: '
        'one or two sentences per prose field, at most three evidence entries per group, no repeated '
        'quotations in rationale or issue_body. Do not echo source files or the input packet.\n'
        + (PRIORITY_GUIDANCE + 'Generic advice to discuss features first is not an upstream requirement. '
           'Set maintainer_required only for an actual applicable upstream rule, citing its evidence. '
           if proactive else '')
        + (PROMPT if packet.get('schema_version') == 2 else '') + json.dumps(packet, ensure_ascii=False))
    if packet.get('schema_version') == 2:
        prompt += ('\nEND OF SOURCE DATA. Output schema reminder:\n' + PROMPT +
                   '\nOnly these files were actually read; checked_paths and quotes MUST use this inventory:\n' +
                   json.dumps({s['repo']: list(s['files']) for s in [packet['target'], *packet['peers']]}))
    if packet.get('mode') == 'source_discovery':
        prompt += ('\nSOURCE DISCOVERY MODE: this real tracked module and nearby tests were selected independently '
                   'of upstream issues. Find at most ONE concrete local bug, reliability, performance, UX or test '
                   'defect with an actual trigger, observed code behavior, expected behavior and a cheap falsifiable '
                   'regression oracle. Merely adding coverage, style cleanup or hypothetical missing features is '
                   'not a demonstrated defect. Return none when the supplied scope is insufficient. No tests have '
                   'run yet. No peers were collected; do not invent comparisons. The feature matrix covers the '
                   'target only; migration_analysis describes the local adaptation. A feature idea needs separate '
                   'cross-project research and cannot enter implementation from this packet. Upstream issue/PR '
                   'absence has not been established; the controller performs duplicate searches after a candidate.')
    if data.get('research_feedback'):
        prompt += ('\nRevise the proposal to address this prior review. Treat it as feedback, not new source evidence. '
                   'When existing_issue is supplied, this proposal continues that discussion, never files a new issue. '
                   'Do not claim verified execution or maintainer approval.\n' + json.dumps(data['research_feedback']))
    if resolved_scope:
        prompt += ('\nRESOLVED LOCAL SCOPE: narrow this investigation to the independently reviewed remaining_scope '
                   'and exclusions in scope_overlap_review. Shared source evidence in scope_overlap_history belongs '
                   'to the prior work and must not be proposed again. This latest resolution constrains earlier '
                   'research and feedback. Produce a revised complete opportunity and matrix with independent '
                   'behavior, affected files, implementation and validation; retain this investigation feature_key. '
                   'No prior research approval is inherited by the revised scope.\n' + json.dumps(resolved_scope))
    pending = data.get('research_pending') or data.get('research', {})
    if pending and pending.get('scope_resolution_digest') != resolved_digest:
        data.setdefault('research_scope_revision_history', []).append({
            'at': now(), 'resolution_digest': resolved_digest, 'previous': pending})
        data.pop('research_pending', None)
        data.pop('research', None)
        engine.store(task_id, data)
        pending = {}
    if data.get('research'):
        data['research_pending'] = pending
        data.pop('research')
        engine.store(task_id, data)
    parse_error = None
    if pending.get('base_sha') == task['base_sha'] and pending.get('fingerprint') == before:
        answer = pending['opportunity']
        model_identity = pending.get('model_identity')
        if (not isinstance(model_identity, dict)
                or not isinstance(model_identity.get('model'), str) or not model_identity['model'].strip()
                or model_identity.get('channel') not in ('hermes', 'opencode', 'dsh')):
            # The current configuration says nothing about who produced an old
            # checkpoint. Missing generation provenance must remain unknown.
            model_identity = {'model': None, 'channel': None}
        else:
            model_identity = dict(model_identity)
    else:
        model_identity = {'model': research_model(p.config),
                          'channel': p.config.get('research', {}).get('model_channel', 'opencode')}
        try:
            answer = research_answer(engine, task_id, 'research', prompt)
        except json.JSONDecodeError as error:
            # Syntax failures share the existing single correction allowance;
            # keep the raw response rather than guessing a repaired JSON value.
            parse_error, answer = error, {}
    if p.fingerprint(task['workspace']) != before:
        raise ValueError('Read-only research changed the workspace')
    try:
        if parse_error is not None:
            raise parse_error
        validate(answer, task, packet)
        if resolved_feature_key and answer.get('decision') == 'candidate' and answer.get('feature_matrix', {}).get('feature_key') != resolved_feature_key:
            raise ValueError('Resolved overlap revision must retain feature_key: ' + resolved_feature_key)
        matrix = build(answer, task, packet) if packet.get('schema_version') == 2 and answer['decision'] == 'candidate' else None
        if matrix and resolved_scope and scope_identity(task['repo'], matrix) == resolved_scope.get('scope'):
            raise ValueError('Resolved overlap requires a revised independent scope, not the unchanged opportunity')
    except ValueError as error:
        if ACTIVE.get():
            ACTIVE.get().count('researched_candidates')
            ACTIVE.get().count('insufficient_evidence')
        # One bounded correction, using exactly the same evidence. Never coerce bad
        # claims into passing evidence or loop indefinitely on an incompatible model.
        previous = parse_error.doc if parse_error is not None else answer
        save(engine.packet_dir(task_id) / 'research-invalid.json', {'answer': previous, 'error': str(error)})
        feedback = str(error) if parse_error is not None else correction_feedback(answer, packet, error)
        correction = (prompt + '\nYour previous response failed validation:\n' + feedback +
                      '\nReturn a corrected COMPLETE JSON object. Recheck every field type and every exact quote. '
                      'Use unknown for unsupported peer capabilities; return none if no grounded candidate remains. '
                      'Do not add facts or claim new searches/tests. Previous response:\n' + json.dumps(previous, ensure_ascii=False))
        model_identity = {'model': research_model(p.config),
                          'channel': p.config.get('research', {}).get('model_channel', 'opencode')}
        answer = research_answer(engine, task_id, 'research-correction', correction)
        if p.fingerprint(task['workspace']) != before:
            raise ValueError('Read-only research correction changed the workspace')
        validate(answer, task, packet)
        if resolved_feature_key and answer.get('decision') == 'candidate' and answer.get('feature_matrix', {}).get('feature_key') != resolved_feature_key:
            raise ValueError('Resolved overlap revision must retain feature_key: ' + resolved_feature_key)
        matrix = build(answer, task, packet) if packet.get('schema_version') == 2 and answer['decision'] == 'candidate' else None
        if matrix and resolved_scope and scope_identity(task['repo'], matrix) == resolved_scope.get('scope'):
            raise ValueError('Resolved overlap requires a revised independent scope, not the unchanged opportunity')
    if matrix:
        persist(engine, task, answer, matrix)
    current = ACTIVE.get()
    if current:
        current.count('researched_candidates')
        current.count('candidate_' + answer.get('category', answer['decision']))
    result = {'base_sha': task['base_sha'], 'fingerprint': before, 'at': now(), 'opportunity': answer,
              'model_identity': model_identity}
    if resolved_digest:
        result['scope_resolution_digest'] = resolved_digest
    if matrix:
        result['feature_id'] = matrix['feature_id']
    save(engine.packet_dir(task_id) / 'opportunity.json', result)
    if answer['decision'] != 'candidate' or answer.get('category') == 'security':
        data['reason'] = answer['reason']
        engine.move(task_id, data, 'SKIPPED')
        return result
    if packet.get('mode') == 'source_discovery':
        from feature_matrix import LOCAL_FIX_CATEGORIES
        if answer.get('category') not in LOCAL_FIX_CATEGORIES:
            data['reason'] = 'Source-only feature idea requires separate cross-project evidence and review'
            engine.move(task_id, data, 'SKIPPED')
            return result  # Existing RESEARCH_ONLY matrix and opportunity retain the idea.
    candidate = {'plan': answer, 'contribution_origin': data.get('contribution_origin', {})}
    # A validated model result is checkpointed before duplicate/reviewer calls.
    data['research_pending'] = result
    engine.store(task_id, data)
    if not engine.duplicates(task_id, candidate, 'issue'):
        if current:
            current.count('duplicate_candidates')
        data['reason'] = candidate.get('skip_reason')
        engine.move(task_id, data, 'SKIPPED')
        return result
    local_candidates = []
    from research_scope import overlaps
    scope_overlaps = [item for item in overlaps(engine.db, task, matrix)
                      if item['prior_state'] not in ('SKIPPED', 'FAILED', 'CLOSED', 'MERGED')] if matrix else []
    for row in engine.db.execute('SELECT task,queue,matrix FROM research_candidates WHERE lower(repo)=lower(?) AND task!=? ORDER BY updated_at DESC LIMIT 20', (task['repo'], task_id)):
        prior = json.loads(row['matrix'])
        local_candidates.append({'task': row['task'], 'queue': row['queue'],
            **{key: prior.get(key) for key in ('feature_key', 'feature_name', 'user_scenario',
                                              'gap_analysis', 'affected_files', 'category')}})
    gate = engine.judge(task_id, 'research-review', {
        'gate': 'Is this valuable, non-duplicate, grounded in actual implementation and project scope? '
                'Check policy evidence, feature absence claims, concrete demand, bounded scope, reproduction '
                'feasibility, validation cost, architecture fit, freshness, and acceptance rationale. '
                'Maintenance response or external contributor acceptance must be supported by actual '
                'supplied observations; missing evidence remains unknown and cannot be invented. '
                'Reject unsupported factual claims, unsourced peer comparisons or any exploitable security disclosure. '
                'Explicitly scoped static evidence and open design choices deferred to maintainers are acceptable for a '
                'discussion proposal; do not demand completed implementation/tests or maintainer approval at this research gate. '
                'Do not approve claims of exactly-once replay or restored external state from transcript-only snapshots. Automated low-risk handling '
                'requires affirmative upstream policy evidence; otherwise require maintainer discussion. '
                'When existing_issue is supplied, the controller will reuse that discussion and wait for maintainer '
                'direction; it will NOT open a new issue. Do not reject that planned reuse as duplicate publication. '
                'Compare supplied local_candidates by user need and implementation scope, not title alone. '
                'If one already covers the same work, reject and return local_duplicate_task naming that supplied task. '
                'Never infer semantic equivalence from keywords alone. '
                'The supplied scope_overlaps contain exact shared source quotes and unique affected files. '
                'Partial overlap is not full duplication: preserve unique work and return local_scope_overlap_task '
                'with approved=false if the scope must be split or explicitly reviewed before installation/implementation. '
                'When scope_overlap_review is resolved, assess the revised independent remainder against that resolution '
                'and its exclusions; do not re-block the original broad scope from a stale research checkpoint. '
                + (PRIORITY_GUIDANCE if proactive else ''),
        'opportunity': answer, 'context': packet, 'existing_issue': candidate.get('existing_issue'),
        'scope_overlap_review': resolved_scope, 'scope_overlap_history': data.get('scope_overlap_history', [])[-1:],
        'local_candidates': local_candidates, 'scope_overlaps': scope_overlaps})
    if gate.get('local_scope_overlap_task'):
        overlap_id = gate['local_scope_overlap_task']
        if gate['approved'] or overlap_id not in {item['task'] for item in scope_overlaps}:
            raise ValueError('Partial overlap must name supplied exact source evidence and require review')
        from research_scope import hold_overlap
        receipt = hold_overlap(engine.db, task_id, overlap_id, gate['reason'])
        data['scope_overlap_review'] = receipt
        engine.store(task_id, data)
        save(engine.packet_dir(task_id) / 'local-scope-overlap.json', receipt)
        return result
    if gate.get('local_duplicate_task'):
        duplicate_id = gate['local_duplicate_task']
        if gate['approved'] or duplicate_id not in {item['task'] for item in local_candidates}:
            raise ValueError('Local semantic duplicate must be a reviewed supplied task with a rejection')
        data['local_duplicate'] = {'task': duplicate_id, 'reason': gate['reason'], 'at': now()}
        data['reason'] = 'Existing local research covers this scope: ' + duplicate_id
        engine.move(task_id, data, 'SKIPPED')
        save(engine.packet_dir(task_id) / 'local-duplicate-review.json', {'candidates': local_candidates, 'review': gate})
        return result
    if not gate['approved']:
        if not data.get('research_revision_attempted'):
            data['research_revision_attempted'] = True
            data['research_feedback'] = {'review': gate, 'existing_issue': candidate.get('existing_issue')}
            data.pop('research_pending', None)
            data.pop('research', None)
            engine.store(task_id, data)
            save(engine.packet_dir(task_id) / 'research-revision-feedback.json', data['research_feedback'])
            return run(engine, task_id, data)
        data['reason'] = gate['reason']
        engine.move(task_id, data, 'SKIPPED')
        return result
    if matrix and candidate.get('existing_issue'):
        matrix['existing_issue'] = {k: candidate['existing_issue'][k] for k in ('number', 'url', 'title')}
    queue, duplicate = persist(engine, task, answer, matrix, reviewed=True, duplicate_clear=True,
                               maintainer=data.get('maintainer_approved', False),
                               proactive=proactive and not candidate.get('existing_issue'))
    if duplicate or (matrix and queue == 'RESEARCH_ONLY'):
        if current:
            current.count('duplicate_candidates' if duplicate else 'insufficient_evidence')
        data['reason'] = 'Local duplicate or insufficient matrix gap evidence'
        engine.move(task_id, data, 'SKIPPED')
        return result
    result['queue'] = queue
    if candidate.get('existing_issue'):
        result['existing_issue'] = candidate['existing_issue']
    if candidate.get('competing_prs'):
        data['competing_prs'] = candidate['competing_prs']
    result['approved'] = True
    data.pop('research_pending', None)
    data['research'] = result
    engine.store(task_id, data)
    save(engine.packet_dir(task_id) / 'opportunity.json', result)
    p.event(task_id, 'Research approved; static evidence only, no remote publication')
    return result


def needs_discussion(opportunity, *, proactive=False):
    # Local preference can remove a generic feature wait, never an upstream rule.
    return ((opportunity['category'] == 'feature' and not proactive) or opportunity['risk'] == 'high'
            or opportunity['policy']['maintainer_required']
            or not opportunity['policy']['automated_pr'])


def research_answer(engine, task_id, purpose, prompt):
    if len(prompt) > limits(engine.p)['context_characters'] + 12000:
        raise TaskBudgetExhausted('research_prompt_characters_exhausted')
    channel = engine.p.config.get('research', {}).get('model_channel', 'opencode')
    if channel in ('opencode', 'dsh'):
        return engine.model(task_id, purpose, prompt)
    if channel == 'hermes':
        return engine.hermes_research(task_id, purpose, prompt)
    raise ValueError('research.model_channel must be opencode, dsh or hermes')
