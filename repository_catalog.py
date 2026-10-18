"""Bounded, read-only admission checks. Research never grants publication rights."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
import subprocess
from urllib.parse import quote

from contribute import command, gh, now, repo_name, save
from repository_policy import inspect_repository
from feature_matrix import file_type

CANDIDATES = '''QwenLM/qwen-code google-gemini/gemini-cli anomalyco/opencode cline/cline
OpenHands/OpenHands Kilo-Org/kilocode aaif-goose/goose SWE-agent/mini-swe-agent
anthropics/claude-code openai/codex Aider-AI/aider zed-industries/zed github/copilot-cli
microsoft/vscode mistralai/mistral-vibe plandex-ai/plandex TabbyML/tabby bytedance/trae-agent
langchain-ai/deepagents langchain-ai/langgraph Pythagora-io/gpt-pilot
modelcontextprotocol/python-sdk modelcontextprotocol/servers e2b-dev/desktop browser-use/browser-use
OpenInterpreter/open-interpreter microsoft/autogen vercel/ai pydantic/pydantic-ai crewAIInc/crewAI
microsoft/semantic-kernel huggingface/smolagents mcp-use/mcp-use NousResearch/hermes-agent
langchain-ai/langchain run-llama/llama_index ag2ai/ag2 BerriAI/litellm OpenBMB/ChatDev julep-ai/julep
agno-agi/agno mastra-ai/mastra FlowiseAI/Flowise langflow-ai/langflow instructor-ai/instructor
e2b-dev/code-interpreter cloudwego/eino stanfordnlp/dspy'''.split()
LICENSES = {'MIT', 'Apache-2.0', 'BSD-3-Clause', 'BSD-2-Clause', 'MPL-2.0', 'GPL-3.0',
            'GPL-3.0-only', 'AGPL-3.0', 'AGPL-3.0-only', 'LGPL-3.0', 'ISC', 'Unlicense'}


def inspect(repo, config):
    record = {'repo': repo, 'url': 'https://github.com/' + repo, 'checked_at': now(),
              'status': 'paused', 'category': 'coding_harness' if repo in CANDIDATES[:20] else 'agent_infrastructure',
              'research_allowed': False, 'automatic_contribution': False,
              'policy': {'automation': 'unconfirmed', 'issue_submission': 'unconfirmed',
                         'pr_submission': 'unconfirmed', 'scope': 'bounded root and discovered guide inspection'},
              'reason': 'Verification incomplete'}
    retired = config.get('repository_checks', {}).get('retired', {})
    if repo.lower() in {r.lower() for r in retired}:
        entry = next(v for k, v in retired.items() if k.lower() == repo.lower())
        return {**record, 'status': 'retired', 'reason': entry['reason'], 'retirement_evidence': entry['evidence']}
    try:
        repo_name(repo)
        def request(*args):
            return gh(*args, timeout=30)
        def raw(args):
            return command(args, timeout=30)
        meta, readme, guide, guide_url, reason = inspect_repository(repo, config.get('repository_checks', {}).get('max_inactive_days', 180), request, raw)
        record.update(canonical=meta['full_name'], default_branch=meta['default_branch'],
                      pushed_at=meta['pushed_at'], updated_at=meta.get('updated_at'),
                      archived=meta['archived'], disabled=meta['disabled'], private=meta.get('private'),
                      license=(meta.get('license') or {}).get('spdx_id'), contributing_url=guide_url)
        if reason or meta.get('private') is not False:
            record['reason'] = reason or 'Public accessibility unconfirmed'
            return record
        sha = request('api', f"repos/{repo}/commits/{meta['default_branch']}")['sha']
        record['sha'] = sha
        if guide_url and '/.github/contents/' in guide_url:
            owner = repo.split('/')[0]
            shared_sha = request('api', f'repos/{owner}/.github/commits/HEAD')['sha']
            shared_path = guide_url.split('/contents/', 1)[1].split('?', 1)[0]
            shared_text = raw(['gh', 'api', f'repos/{owner}/.github/contents/{shared_path}?ref={shared_sha}',
                               '-H', 'Accept: application/vnd.github.raw+json'])
            record['shared_policy'] = {'repo': owner + '/.github', 'sha': shared_sha, 'path': shared_path,
                                       'url': f'https://github.com/{owner}/.github/blob/{shared_sha}/{shared_path}',
                                       'sha256': hashlib.sha256(shared_text.encode()).hexdigest(),
                                       'excerpt': shared_text[:2000]}
        tree = request('api', f'repos/{repo}/git/trees/{sha}?recursive=1')
        if tree.get('truncated'):
            record['reason'] = 'Source inventory truncated; bounded verification cannot establish coverage'
            return record
        blobs = [b for b in tree['tree'] if b['type'] == 'blob']
        paths = {b['path']: b for b in blobs}
        inventories = {kind: [p for p in paths if file_type(p) == kind] for kind in ('source', 'test', 'documentation')}
        record['inventory'] = {k: {'count': len(v), 'examples': v[:10]} for k, v in inventories.items()}
        selected = []
        for kind, options in inventories.items():
            useful = [p for p in options if paths[p].get('size', 1000000) <= 40000 and
                      not any(part in ('third_party', 'vendor', 'node_modules', 'vendored') for part in Path(p).parts)]
            if useful:
                def sample_order(path):
                    basename = Path(path).name.lower()
                    explicit_test = bool(re.search(r'^test_.+\.|_test\.|\.(test|spec)\.', basename))
                    return (0 if kind != 'test' or explicit_test else 1, path.count('/'), len(path))
                selected.append(min(useful, key=sample_order))
        policy_paths = [p for p in paths if p.lower() in ('readme.md', 'contributing.md', 'agents.md',
                         'security.md', '.github/security.md', '.github/contributing.md', 'docs/contributing.md',
                         '.github/issue_template/config.yml', '.github/pull_request_template.md', 'license', 'license.md')]
        if guide_url and '/contents/' in guide_url:
            policy_paths.append(guide_url.split('/contents/', 1)[1].split('?', 1)[0])
        selected = list(dict.fromkeys(selected + policy_paths))[:15]
        record['files'] = {}
        for path in selected:
            if path not in paths or paths[path].get('size', 1000000) > 60000:
                continue
            text = raw(['gh', 'api', f"repos/{repo}/contents/{quote(path, safe='/')}?ref={sha}",
                            '-H', 'Accept: application/vnd.github.raw+json'])
            record['files'][path] = {'sha256': hashlib.sha256(text.encode()).hexdigest(),
                                    'characters': len(text), 'file_type': file_type(path),
                                    'url': f'https://github.com/{repo}/blob/{sha}/{quote(path, safe="/")}',
                                    'excerpt': text[:600],
                                    'policy_excerpts': [line[:600] for line in text.splitlines()
                                                        if any(w in line.lower() for w in ('automat', 'contribut', 'human', 'ready', 'discuss', 'ai-generated'))][:12]}
        kinds = {v['file_type'] for v in record['files'].values()}
        if record['license'] not in LICENSES:
            record.update(status='rejected', reason='Open-source license not established by GitHub license metadata; manual license review required')
        elif not {'source', 'test', 'documentation'}.issubset(kinds):
            record.update(status='rejected', reason='Usable source, test and documentation samples not all verified')
        else:
            record.update(status='research_only', research_allowed=True,
                          reason='Pinned public source/test/documentation verified; automated contribution NOT granted')
        record['policy'].update(config.get('repository_checks', {}).get('policies', {}).get(repo, {}))
        record['policy']['contribution_allowlisted'] = repo in config['repositories']
        record['policy']['decision'] = 'Existing contribution allowlist still requires live health, task policy and publication gates; catalog never authorizes publication'
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired, KeyError, TypeError) as error:
        status = re.search(r'HTTP (\d{3})', str(error))
        record.update(status='paused', research_allowed=False, reason='Verification failed: ' + type(error).__name__ + (' HTTP ' + status[1] if status else ''))
    return record


def verify_catalog(root, config, repositories=None):
    path = Path(root) / config.get('repository_catalog', 'repository-catalog.json')
    previous = json.loads(path.read_text(encoding='utf-8'))['repositories'] if path.exists() else []
    saved = {item['repo']: item for item in previous}
    repositories = list(dict.fromkeys(repositories or [*CANDIDATES, *saved, *config.get('repository_checks', {}).get('retired', {})]))
    if len(repositories) > 80:
        raise ValueError('Catalog verification is capped at 80 repositories per batch')
    def check(repo):
        if saved.get(repo, {}).get('status') == 'retired':
            return saved[repo]  # Explicit retirement cannot be silently reversed by refreshing a catalog.
        return inspect(repo, config)
    # Each check has at most 25 reads, no pagination or mutations; concurrency capped at four.
    with ThreadPoolExecutor(max_workers=4) as pool:
        for record in pool.map(check, repositories):
            saved[record['repo']] = record
            print(record['repo'] + ': ' + record['status'] + ' - ' + record['reason'], flush=True)
            save(path, {'schema_version': 1, 'checked_at': now(), 'repositories': list(saved.values())})
    return dict(Counter(r['status'] for r in saved.values()))


def research_pool(pipeline, *, respect_peer_filter=True):
    path = pipeline.root / pipeline.config.get('repository_catalog', 'repository-catalog.json')
    retired = {r.lower() for r in pipeline.config.get('repository_checks', {}).get('retired', {})}
    if not path.exists():
        # Compatibility with allowlist-only configurations: live health checked.
        return [r for r in pipeline.effective_repositories() if r.lower() not in retired]
    catalog = json.loads(path.read_text(encoding='utf-8'))
    allowed = pipeline.config.get('peer_repositories') if respect_peer_filter else None
    result = []
    for item in catalog['repositories']:
        checked = dt.datetime.fromisoformat(item['checked_at'])
        fresh = (dt.datetime.now(dt.timezone.utc) - checked).total_seconds() < pipeline.config.get('research', {}).get('catalog_max_age_days', 30) * 86400
        if (fresh and item['repo'].lower() not in retired and item.get('research_allowed') is True
                and item['status'] in ('active', 'research_only') and not item.get('archived') and not item.get('disabled')
                and (allowed is None or item['repo'] in allowed)):
            result.append(item['repo'])
    return list(dict.fromkeys(result))
