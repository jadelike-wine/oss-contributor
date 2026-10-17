"""Bounded, read-only opportunity research, using the existing model and gates."""
import json
import hashlib
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from contribute import command, execute, gh, now, save
from telemetry import BudgetExhausted


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


def select_paths(paths, focus, count, content_matches=()):
    from feature_matrix import file_type
    words = focus_terms(focus)
    matches = set(content_matches)
    def relevance(path):
        runtime = bool(re.search(r'(^|/)(src|lib|core|agents?)/', path.lower()))
        housekeeping = path.startswith(('.github/', 'scripts/', 'docs/', 'examples/'))
        helper = Path(path).name in ('__init__.py', '__main__.py', 'conftest.py')
        tokens = re.findall(r'[a-z0-9]+', re.sub(r'([a-z])([A-Z])', r'\1_\2', path).lower())
        score = sum(any(t.startswith(w) for t in tokens) for w in words) + 3 * (path in matches)
        return (helper, -score, housekeeping, not runtime, len(path), path)
    ordered = sorted((p for p in paths if file_type(p) != 'configuration'), key=relevance)
    selected = []
    # Reserve coverage for executable evidence and documentation, then fill by relevance.
    for kind in ('source', 'test', 'documentation'):
        match = next((p for p in ordered if file_type(p) == kind), None)
        if match and len(selected) < count:
            selected.append(match)
    return (selected + [p for p in ordered if p not in selected])[:count]


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
            raise BudgetExhausted('research_network_requests_exhausted')
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
            # Include nested rules applying to selected code paths, within the target read cap.
            matches = search_target(work, task['base_sha'], task['title'], paths)
            selected = select_paths(paths, task['title'], options['target_files'], matches)
            policy_names += [x for x in paths if x.endswith('/AGENTS.md') and
                             any(y.startswith(x.rsplit('/', 1)[0] + '/') for y in selected)]
            policy_names = list(dict.fromkeys(x for x in policy_names if x in paths))
            while True:
                if len(policy_names) >= options['target_files']:
                    raise BudgetExhausted('target_policy_file_budget_exhausted')
                selected = policy_names + select_paths([x for x in paths if x not in policy_names],
                    task['title'], options['target_files'] - len(policy_names), matches)
                nested = [x for x in paths if x.endswith('/AGENTS.md') and x not in policy_names
                          and any(y.startswith(x.rsplit('/', 1)[0] + '/') for y in selected)]
                if not nested:
                    break
                policy_names.extend(nested)
            files = {path: command(['git', 'show', task['base_sha'] + ':' + path], cwd=work)[:options['file_characters']]
                     for path in selected}
            packet['policy'] = {path: files[path] for path in policy_names}
            packet['target'] = {'repo': task['repo'], 'sha': task['base_sha'], 'files': files, 'checked_paths': selected,
                                'content_search': {'terms': focus_terms(task['title']), 'matched_paths': matches,
                                                   'scope': 'eligible tracked baseline text; matches are not absence proof'}}
            persist()
        for peer in packet['selected_peers']:
            if any(s['repo'] == peer and not s.get('error') for s in packet['peers']):
                continue
            try:
                source = snapshot(peer, task['title'], options, request, raw)
            except BudgetExhausted:
                raise
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired, KeyError, TypeError) as error:
                source = {'repo': peer, 'sha': None, 'files': {}, 'error': type(error).__name__, 'status': 'unknown'}
            packet['peers'] = [s for s in packet['peers'] if s['repo'] != peer] + [source]
            persist()
        if 'recent_issues_and_prs' not in packet:
            packet['recent_issues_and_prs'] = [
                {k: str(item.get(k) or '')[:2000] for k in ('number', 'title', 'body', 'html_url', 'state')}
                for item in request('api', f"repos/{task['repo']}/issues?state=all&sort=updated&per_page=20")]
            persist()
        if 'recent_releases' not in packet:
            packet['recent_releases'] = [
                {k: str(item.get(k) or '')[:2000] for k in ('tag_name', 'body', 'html_url')}
                for item in request('api', f"repos/{task['repo']}/releases?per_page=3")]
        if len(json.dumps(packet, ensure_ascii=False)) > options['context_characters']:
            raise BudgetExhausted('research_context_characters_exhausted')
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
    # Quotes are checked against actual pinned files, not model-provided URLs.
    for group in ('evidence', 'policy_evidence'):
        evidence = answer.get(group)
        if not isinstance(evidence, list) or not evidence:
            raise ValueError('Missing research evidence: ' + group)
        for item in evidence:
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
            item['sha'] = sha
            from feature_matrix import file_type
            item['file_type'] = file_type(item['path'])
            item['url'] = f"https://github.com/{item['repo']}/blob/{sha}/{quote(item['path'], safe='/')}"
    if not any(x['repo'] == task['repo'] for x in answer['evidence']):
        raise ValueError('Target implementation evidence required')
    if answer['category'] == 'feature' and not any(x['repo'] != task['repo'] for x in answer['evidence']):
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
    for claim in claims:
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
    before = p.fingerprint(task['workspace'])
    packet = collect(engine, task)
    from feature_matrix import PROMPT, build, persist
    from telemetry import ACTIVE
    prompt = ('Prioritize valuable feature development; bugs are supplementary. Research ONE valuable opportunity, or return none. Read actual target code, docs, tests, '
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
        'evidence and policy_evidence arrays of {repo,path,quote} with EXACT source excerpts. '
        'Prefer short contiguous quotes under 200 characters; never join separated lines or paraphrase a quote. '
        'policy={automated_issue:bool,automated_pr:bool,maintainer_required:bool,reason:string}. '
        'automated_pr describes whether upstream POLICY permits automated implementation/PRs once all '
        'required maintainer approvals exist; use maintainer_required for missing design approval. '
        'An explicit automation ban or unverifiable policy means automated_pr=false, which approval cannot override. '
        'Describe cost in effort. acceptance means maintainer acceptance LIKELIHOOD with evidence and rationale, '
        'not test acceptance criteria. State who requested this or which contribution priority it fits; '
        'do not invent maintainer approval. Keep test acceptance criteria in validation. Include concrete validation commands, '
        'expected result and affected files in scope. Policy evidence must quote applicable contribution rules. '
        'Features, architecture changes, uncertain designs and non-low risks always need maintainer direction '
        'before any implementation. Small proven bugs/tests/performance/UX can proceed only if policy permits. '
        'issue_body must be English, accurate about static evidence and pending tests. At most 25 source reads.\n'
        + (PROMPT if packet.get('schema_version') == 2 else '') + json.dumps(packet, ensure_ascii=False))
    if packet.get('schema_version') == 2:
        prompt += ('\nEND OF SOURCE DATA. Output schema reminder:\n' + PROMPT +
                   '\nOnly these files were actually read; checked_paths and quotes MUST use this inventory:\n' +
                   json.dumps({s['repo']: list(s['files']) for s in [packet['target'], *packet['peers']]}))
    if data.get('research_feedback'):
        prompt += ('\nRevise the proposal to address this prior review. Treat it as feedback, not new source evidence. '
                   'When existing_issue is supplied, this proposal continues that discussion, never files a new issue. '
                   'Do not claim verified execution or maintainer approval.\n' + json.dumps(data['research_feedback']))
    pending = data.get('research_pending') or data.get('research', {})
    if data.get('research'):
        data['research_pending'] = pending
        data.pop('research')
        engine.store(task_id, data)
    if pending.get('base_sha') == task['base_sha'] and pending.get('fingerprint') == before:
        answer = pending['opportunity']
    else:
        answer = research_answer(engine, task_id, 'research', prompt)
    if p.fingerprint(task['workspace']) != before:
        raise ValueError('Read-only research changed the workspace')
    try:
        validate(answer, task, packet)
        matrix = build(answer, task, packet) if packet.get('schema_version') == 2 and answer['decision'] == 'candidate' else None
    except ValueError as error:
        if ACTIVE.get():
            ACTIVE.get().count('researched_candidates')
            ACTIVE.get().count('insufficient_evidence')
        # One bounded correction, using exactly the same evidence. Never coerce bad
        # claims into passing evidence or loop indefinitely on an incompatible model.
        save(engine.packet_dir(task_id) / 'research-invalid.json', {'answer': answer, 'error': str(error)})
        correction = (prompt + '\nYour previous response failed validation:\n' + correction_feedback(answer, packet, error) +
                      '\nReturn a corrected COMPLETE JSON object. Recheck every field type and every exact quote. '
                      'Use unknown for unsupported peer capabilities; return none if no grounded candidate remains. '
                      'Do not add facts or claim new searches/tests. Previous response:\n' + json.dumps(answer, ensure_ascii=False))
        answer = research_answer(engine, task_id, 'research-correction', correction)
        if p.fingerprint(task['workspace']) != before:
            raise ValueError('Read-only research correction changed the workspace')
        validate(answer, task, packet)
        matrix = build(answer, task, packet) if packet.get('schema_version') == 2 and answer['decision'] == 'candidate' else None
    if matrix:
        persist(engine, task, answer, matrix)
    current = ACTIVE.get()
    if current:
        current.count('researched_candidates')
        current.count('candidate_' + answer.get('category', answer['decision']))
    result = {'base_sha': task['base_sha'], 'fingerprint': before, 'at': now(), 'opportunity': answer}
    if matrix:
        result['feature_id'] = matrix['feature_id']
    save(engine.packet_dir(task_id) / 'opportunity.json', result)
    if answer['decision'] != 'candidate' or answer.get('category') == 'security':
        data['reason'] = answer['reason']
        engine.move(task_id, data, 'SKIPPED')
        return result
    candidate = {'plan': answer}
    # A validated model result is checkpointed before duplicate/reviewer calls.
    data['research_pending'] = result
    engine.store(task_id, data)
    if not engine.duplicates(task_id, candidate, 'issue'):
        if current:
            current.count('duplicate_candidates')
        data['reason'] = candidate.get('skip_reason')
        engine.move(task_id, data, 'SKIPPED')
        return result
    gate = engine.judge(task_id, 'research-review', {
        'gate': 'Is this valuable, non-duplicate, grounded in actual implementation and project scope? '
                'Check policy evidence, feature absence claims, cost and acceptance rationale. '
                'Reject unsupported factual claims, unsourced peer comparisons or any exploitable security disclosure. '
                'Explicitly scoped static evidence and open design choices deferred to maintainers are acceptable for a '
                'discussion proposal; do not demand completed implementation/tests or maintainer approval at this research gate. '
                'Do not approve claims of exactly-once replay or restored external state from transcript-only snapshots. Automated low-risk handling '
                'requires affirmative upstream policy evidence; otherwise require maintainer discussion. '
                'When existing_issue is supplied, the controller will reuse that discussion and wait for maintainer '
                'direction; it will NOT open a new issue. Do not reject that planned reuse as duplicate publication.',
        'opportunity': answer, 'context': packet, 'existing_issue': candidate.get('existing_issue')})
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
                               maintainer=data.get('maintainer_approved', False))
    if duplicate or (matrix and queue == 'RESEARCH_ONLY'):
        if current:
            current.count('duplicate_candidates' if duplicate else 'insufficient_evidence')
        data['reason'] = 'Local duplicate or insufficient matrix gap evidence'
        engine.move(task_id, data, 'SKIPPED')
        return result
    result['queue'] = queue
    if candidate.get('existing_issue'):
        result['existing_issue'] = candidate['existing_issue']
    result['approved'] = True
    data.pop('research_pending', None)
    data['research'] = result
    engine.store(task_id, data)
    save(engine.packet_dir(task_id) / 'opportunity.json', result)
    p.event(task_id, 'Research approved; static evidence only, no remote publication')
    return result


def needs_discussion(opportunity):
    return (opportunity['category'] == 'feature' or opportunity['risk'] != 'low'
            or opportunity['policy']['maintainer_required']
            or not opportunity['policy']['automated_pr'])


def research_answer(engine, task_id, purpose, prompt):
    if len(prompt) > limits(engine.p)['context_characters'] + 12000:
        raise BudgetExhausted('research_prompt_characters_exhausted')
    channel = engine.p.config.get('research', {}).get('model_channel', 'opencode')
    if channel == 'opencode':
        return engine.model(task_id, purpose, prompt)
    if channel == 'hermes':
        return engine.hermes_research(task_id, purpose, prompt)
    raise ValueError('research.model_channel must be opencode or hermes')
