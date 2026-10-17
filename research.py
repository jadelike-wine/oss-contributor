"""Bounded, read-only opportunity research, using the existing model and gates."""
import json
from pathlib import Path
from urllib.parse import quote

from contribute import command, gh, now, save


CATEGORIES = ('feature', 'performance', 'security', 'ux', 'reliability', 'bug', 'tests')
FIELDS = ('title', 'background', 'users', 'expected_effect', 'scope', 'validation',
          'risks', 'effort', 'acceptance', 'comparison', 'issue_body')


def snapshot(repo):
    """Official evidence only. Pin source reads; never treat API failure as absence."""
    meta = gh('api', f'repos/{repo}')
    sha = gh('api', f"repos/{repo}/commits/{meta['default_branch']}")['sha']
    tree = gh('api', f'repos/{repo}/git/trees/{sha}?recursive=1')
    if tree.get('truncated'):
        raise ValueError('Research source tree is truncated: ' + repo)
    paths = [x['path'] for x in tree['tree'] if x['type'] == 'blob']
    readme = next((p for p in paths if p.lower() == 'readme.md'), None)
    files = {}
    if readme:
        files[readme] = command(['gh', 'api', f'repos/{repo}/contents/{readme}?ref={sha}',
                                '-H', 'Accept: application/vnd.github.raw+json'])
    return {'repo': repo, 'sha': sha, 'paths': paths, 'files': files}


def collect(engine, task):
    p = engine.p
    # A rotating peer keeps requests bounded and compares different harnesses over time.
    peers = [r for r in p.effective_repositories() if r != task['repo']]
    peer = peers[sum(task['id'].encode()) % len(peers)] if peers else None
    sources = [snapshot(peer)] if peer else []
    if sources:
        # Prefer focus-related paths but expose a bounded inventory to the selector.
        words = set(task['title'].lower().replace('-', ' ').split()) - {'the', 'and', 'one', 'with', 'from'}
        sources[0]['paths'].sort(key=lambda path: (-sum(w in path.lower() for w in words if len(w) > 3), len(path), path))
        sources[0]['paths'] = sources[0]['paths'][:600]
    work = Path(task['workspace'])
    policy = {}
    for name in dict.fromkeys([p.contributing_path(task['repo']), 'CONTRIBUTING.md',
                               'AGENTS.md', 'SECURITY.md', '.github/SECURITY.md', 'docs/SECURITY.md']):
        file = (work / name).resolve()
        if file.is_relative_to(work.resolve()) and file.is_file():
            policy[name] = file.read_text(encoding='utf-8')
    packet = {'at': now(), 'task': task, 'policy': policy, 'peers': sources,
              'recent_issues_and_prs': gh('api', f"repos/{task['repo']}/issues?state=all&sort=updated&per_page=30"),
              'recent_releases': gh('api', f"repos/{task['repo']}/releases?per_page=3")}
    packet['recent_issues_and_prs'] = [
        {key: (str(item[key])[:5000] if key == 'body' else item[key])
         for key in ('number', 'title', 'body', 'html_url', 'state', 'pull_request') if key in item}
        for item in packet['recent_issues_and_prs']]
    packet['recent_releases'] = [
        {key: str(item.get(key) or '')[:6000] for key in ('tag_name', 'body', 'html_url')}
        for item in packet['recent_releases']]
    if sources:
        request = engine.model(task['id'], 'research-sources',
            'Select up to FOUR useful PEER code, test, design or changelog files from peer.paths '
            'to compare with the target focus. This step only selects remote peer material: do not read '
            'the local checkout or return target paths. Use [] if no useful peer evidence exists. '
            'No edits, shell, network or delegation. Return ONLY JSON {"paths":["exact path from peer.paths"]}. '
            'Context:\n' + json.dumps({'focus': task['title'], 'target_repo': task['repo'], 'peer': sources[0]}, ensure_ascii=False))
        paths = request.get('paths')
        if not isinstance(paths, list) or len(paths) > 4 or any(not isinstance(x, str) or x not in sources[0]['paths'] for x in paths):
            raise ValueError('Invalid peer evidence paths')
        for path in dict.fromkeys(paths):
            sources[0]['files'][path] = command(['gh', 'api',
                f"repos/{peer}/contents/{quote(path, safe='/')}?ref={sources[0]['sha']}",
                '-H', 'Accept: application/vnd.github.raw+json'])
        # The path inventory was only for discovery; keep the final packet bounded.
        sources[0].pop('paths')
    save(engine.packet_dir(task['id']) / 'research-context.json', packet)
    return packet


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
                text = file.read_text(encoding='utf-8') if file.is_relative_to(work) and file.is_file() else ''
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
            item['url'] = f"https://github.com/{item['repo']}/blob/{sha}/{quote(item['path'], safe='/')}"
    if not any(x['repo'] == task['repo'] for x in answer['evidence']):
        raise ValueError('Target implementation evidence required')
    if answer['category'] == 'feature' and not any(x['repo'] != task['repo'] for x in answer['evidence']):
        raise ValueError('Feature comparisons require actual peer implementation evidence')


def run(engine, task_id, data):
    """No public side effects. Persist the result and let PLAN consume it later."""
    p = engine.p
    p.require_contribution(p.task(task_id))
    p.prepare(task_id)
    task = p.task(task_id)
    before = p.fingerprint(task['workspace'])
    packet = collect(engine, task)
    prompt = ('Research ONE valuable opportunity, or return none. Read actual target code, docs, tests, '
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
        + json.dumps(packet, ensure_ascii=False))
    answer = engine.model(task_id, 'research', prompt)
    if p.fingerprint(task['workspace']) != before:
        raise ValueError('Read-only research changed the workspace')
    validate(answer, task, packet)
    result = {'base_sha': task['base_sha'], 'fingerprint': before, 'at': now(), 'opportunity': answer}
    save(engine.packet_dir(task_id) / 'opportunity.json', result)
    if answer['decision'] != 'candidate' or answer.get('category') == 'security':
        data['reason'] = answer['reason']
        engine.move(task_id, data, 'SKIPPED')
        return result
    candidate = {'plan': answer}
    if not engine.duplicates(task_id, candidate, 'issue'):
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
    result['approved'] = True
    data['research'] = result
    engine.store(task_id, data)
    save(engine.packet_dir(task_id) / 'opportunity.json', result)
    p.event(task_id, 'Research approved; static evidence only, no remote publication')
    return result


def needs_discussion(opportunity):
    return (opportunity['category'] == 'feature' or opportunity['risk'] != 'low'
            or opportunity['policy']['maintainer_required']
            or not opportunity['policy']['automated_pr'])
