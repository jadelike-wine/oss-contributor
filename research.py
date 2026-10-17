"""Bounded, read-only opportunity research, using the existing model and gates."""
import json
import hashlib
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from contribute import command, gh, now, save
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


def select_paths(paths, focus, count):
    from feature_matrix import file_type
    words = set(re.findall(r'[a-z0-9_]+', focus.lower())) - {'the', 'and', 'one', 'with', 'from', 'research', 'compare'}
    for word, related in {'session': ('history', 'conversation', 'state', 'agent'),
                          'recovery': ('resume', 'checkpoint', 'persistence'),
                          'context': ('compact', 'compress', 'message')}.items():
        if word in words:
            words.update(related)
    def relevance(path):
        runtime = bool(re.search(r'(^|/)(src|lib|core|agents?)/', path.lower()))
        housekeeping = path.startswith(('.github/', 'scripts/', 'docs/', 'examples/'))
        return (-sum(w in path.lower() for w in words if len(w) > 3), housekeeping, not runtime, len(path), path)
    ordered = sorted(paths, key=relevance)
    selected = []
    # Reserve coverage for executable evidence and documentation, then fill by relevance.
    for kind in ('source', 'test', 'documentation'):
        match = next((p for p in ordered if file_type(p) == kind), None)
        if match and len(selected) < count:
            selected.append(match)
    design = next((p for p in ordered if any(w in p.lower() for w in ('changelog', 'architecture', 'design')) and p not in selected), None)
    if design and len(selected) < count:
        selected.append(design)
    return (selected + [p for p in ordered if p not in selected])[:count]


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
            selected = select_paths(paths, task['title'], options['target_files'])
            policy_names += [x for x in paths if x.endswith('/AGENTS.md') and
                             any(y.startswith(x.rsplit('/', 1)[0] + '/') for y in selected)]
            policy_names = list(dict.fromkeys(x for x in policy_names if x in paths))
            if len(policy_names) >= options['target_files']:
                raise BudgetExhausted('target_policy_file_budget_exhausted')
            selected = list(dict.fromkeys(policy_names + selected))[:options['target_files']]
            files = {path: command(['git', 'show', task['base_sha'] + ':' + path], cwd=work)[:options['file_characters']]
                     for path in selected}
            packet['policy'] = {path: files[path] for path in policy_names}
            packet['target'] = {'repo': task['repo'], 'sha': task['base_sha'], 'files': files, 'checked_paths': selected}
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
        'not evidence: search all existing implementations, tests and design docs; consider project scope. '
        'Inspect the supplied user feedback, issues and PRs; the controller will also search open AND closed '
        'duplicates. Never invent execution or negative search evidence. No edits, shell, publishing or delegation. '
        'For exploitable security findings return decision=private with a non-sensitive reason; never include '
        'exploit details in title/search queries/public body. Follow SECURITY.md and private disclosure policy. '
        'Return ONLY JSON with decision=candidate|none|private, reason. For candidate include category='
        'feature|performance|security|ux|reliability|bug|tests, risk=low|medium|high, '
        + ', '.join(FIELDS) + ', duplicate_queries (1-2 precise searches), '
        'evidence and policy_evidence arrays of {repo,path,quote} with EXACT source excerpts, '
        'policy={automated_issue:bool,automated_pr:bool,maintainer_required:bool,reason:string}. '
        'automated_pr describes whether upstream POLICY permits automated implementation/PRs once all '
        'required maintainer approvals exist; use maintainer_required for missing design approval. '
        'An explicit automation ban or unverifiable policy means automated_pr=false, which approval cannot override. '
        'Describe cost in effort and acceptance likelihood WITH rationale. Include concrete validation commands, '
        'expected result and affected files in scope. Policy evidence must quote applicable contribution rules. '
        'Features, architecture changes, uncertain designs and non-low risks always need maintainer direction '
        'before any implementation. Small proven bugs/tests/performance/UX can proceed only if policy permits. '
        'issue_body must be English, accurate about static evidence and pending tests. At most 25 source reads.\n'
        + (PROMPT if packet.get('schema_version') == 2 else '') + json.dumps(packet, ensure_ascii=False))
    pending = data.get('research_pending', {})
    if pending.get('base_sha') == task['base_sha'] and pending.get('fingerprint') == before:
        answer = pending['opportunity']
    else:
        answer = engine.model(task_id, 'research', prompt)
    if p.fingerprint(task['workspace']) != before:
        raise ValueError('Read-only research changed the workspace')
    try:
        validate(answer, task, packet)
        matrix = build(answer, task, packet) if packet.get('schema_version') == 2 and answer['decision'] == 'candidate' else None
    except ValueError:
        if ACTIVE.get():
            ACTIVE.get().count('researched_candidates')
            ACTIVE.get().count('insufficient_evidence')
        raise
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
                'Reject uncertainty, unsourced peer comparisons or any exploitable security disclosure. Automated low-risk handling '
                'requires affirmative upstream policy evidence; otherwise require maintainer discussion.',
        'opportunity': answer, 'context': packet})
    if not gate['approved']:
        data['reason'] = gate['reason']
        engine.move(task_id, data, 'SKIPPED')
        return result
    queue, duplicate = persist(engine, task, answer, matrix, reviewed=True, duplicate_clear=True,
                               maintainer=data.get('maintainer_approved', False))
    if duplicate or (matrix and queue == 'RESEARCH_ONLY'):
        if current:
            current.count('duplicate_candidates' if duplicate else 'insufficient_evidence')
        data['reason'] = 'Local duplicate or insufficient matrix gap evidence'
        engine.move(task_id, data, 'SKIPPED')
        return result
    result['queue'] = queue
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
