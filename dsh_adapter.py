"""Isolated, least-capability adapter for the DeepSeek Harness headless CLI.

The desktop bundle exposes the ordinary CLI through Electron's Node mode.
Configuration contains credential *references* only; the key stays in env.
"""
import json
import os
from pathlib import Path
import re
import shutil
import uuid


class DSHError(RuntimeError):
    """The harness did not provide a verifiable completed response."""


class DSHProtocolError(ValueError):
    """A stream cannot prove completion or violated the tool boundary."""


READ_TOOLS = ('read', 'glob', 'grep')
EDIT_TOOLS = READ_TOOLS + ('edit', 'write')
_DISABLED = (
    'tool-plugin-manager', 'plugin-manager', 'hmr', 'settings', 'config-editor',
    'session-title-llm', 'session-telemetry-otel', 'otel', 'agent-instructions',
    'tool-bash', 'tool-pwsh', 'tool-jobs', 'tool-working-directory', 'tool-skill',
    'skill', 'skill-filesystem', 'commands', 'command-feedback', 'command-goal',
    'goal', 'goal-round-driver', 'plan-mode', 'command-compact', 'subagent',
    'subagent-spawn-in-process', 'subagent-fork-in-process', 'tool-subagent-control',
    'tool-subagent', 'tool-subagent-fork', 'workflow-ptc', 'tool-workflow',
    'tool-todo', 'tool-goal', 'web', 'web-search-deepseek', 'web-deepseek-api',
    'web-fetch-http', 'tool-web', 'mcp-resources', 'llm-deepseek',
    'llm-deepseek-account', 'deepseek-account', 'authorization', 'credentials',
    'remote-access', 'typert-gateway', 'tool-executor-ptc',
)


def discover_cli(config):
    """Return an argv prefix and environment additions, never a shell shim."""
    settings = config.get('dsh', {})
    install = Path(settings.get('desktop_install_dir') or
                   Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) /
                   'Programs' / 'DeepSeek Harness')
    exe = install / 'DeepSeek Harness.exe'
    archive = install / 'resources' / 'app.asar'
    if exe.is_file() and archive.is_file():
        entry = archive / 'dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js'
        return [str(exe), '--expose-internals', str(entry)], {'ELECTRON_RUN_AS_NODE': '1'}
    executable = settings.get('executable') or shutil.which('dsh')
    if executable and Path(executable).suffix.lower() not in ('.cmd', '.bat', '.ps1'):
        return [str(executable)], {}
    raise DSHError('DSH CLI not found; configure dsh.desktop_install_dir or a native executable')


def _provider(config, provider, env):
    settings = config.get('dsh', {})
    base_url = settings.get('base_url')
    key_name = settings.get('api_key_env')
    key = os.environ.get(key_name) if isinstance(key_name, str) else None
    if key_name and (not isinstance(key_name, str) or
                     not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key_name) or not key):
        raise DSHError('The configured DSH credential environment variable is unavailable')
    if not base_url or not key:
        source = Path(settings.get('provider_config_path') or
                      Path.home() / '.config' / 'opencode' / 'opencode.json')
        try:
            document = json.loads(source.read_text(encoding='utf-8-sig'))
            options = document.get('provider', {}).get(provider, {}).get('options', {})
        except (OSError, ValueError, AttributeError) as exc:
            raise DSHError('Cannot read the configured provider metadata') from None
        base_url = base_url or options.get('baseURL')
        if not key:
            key = options.get('apiKey')
            if isinstance(key, str) and key.startswith('{env:') and key.endswith('}'):
                key = os.environ.get(key[5:-1])
    if not isinstance(base_url, str) or not base_url.startswith(('https://', 'http://')):
        raise DSHError('DSH requires an explicit HTTP(S) provider base_url')
    if not isinstance(key, str) or not key.strip():
        raise DSHError('DSH provider credential is unavailable')
    # A fixed private reference avoids ever serializing a source credential.
    env['OSS_DSH_API_KEY'] = key
    return base_url


def _guard_source(allowed, provider, model):
    # The runner depends on ossDshGuard, so a guard import/mount failure cannot
    # start an unguarded model. Restrictions affect both visibility and dispatch.
    return '''export const name = 'oss-dsh-guard';
export const inject = ['tools', 'agents'];
const allowed = %s;
const provider = %s;
const model = %s;
export function apply(ctx) {
  const verify = (agent) => {
    const names = [...agent.ctx.tools.view(agent).visible.keys()].sort();
    if (JSON.stringify(names) !== JSON.stringify([...allowed].sort()))
      throw new Error('OSS_DSH_TOOL_BOUNDARY');
  };
  ctx.on('agent/created', ({agent}) => {
    agent.ctx.tools.restrict({allow: allowed});
    verify(agent);
  });
  ctx.on('agent/pre-step', async ({agent, modelSelection}, next) => {
    verify(agent);
    if (modelSelection.provider !== provider || modelSelection.model !== model)
      throw new Error('OSS_DSH_MODEL_BOUNDARY');
    return next();
  });
  ctx.provide('ossDshGuard', {});
}
''' % (json.dumps(list(allowed)), json.dumps(provider), json.dumps(model))


def build_invocation(config, root, workspace, prompt_path, *, model, edit=False, no_tools=False):
    """Prepare argv/env/stdin for execute(cwd=workspace), without running a model.

    Every call has a fresh home beneath root/state/dsh: no desktop profiles,
    global patches, old sessions, or fallback model selection are inherited.
    """
    provider, separator, model_id = model.partition('/')
    if not separator or not provider or not model_id:
        raise DSHError('DSH model must be provider/model')
    settings = config.get('dsh', {})
    root, workspace, prompt_path = Path(root).resolve(), Path(workspace).resolve(), Path(prompt_path).resolve()
    prompt = prompt_path.read_text(encoding='utf-8')
    if not prompt.strip():
        raise DSHError('DSH prompt must not be empty')
    args, additions = discover_cli(config)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('DSH_') and key not in ('NODE_OPTIONS', 'OPENCODE_CONFIG_CONTENT')}
    env.update(additions)
    base_url = _provider(config, provider, env)
    home = root / 'state' / 'dsh' / uuid.uuid4().hex
    home.mkdir(parents=True, exist_ok=False)
    env.update(DSH_HOME=str(home), DSH_TELEMETRY_MODE='DISABLED',
               DSH_PERMISSION_MODE='workspace-write' if edit else 'read-only')
    ca = root / 'state' / 'windows-ca.pem'
    if ca.is_file() and not env.get('NODE_EXTRA_CA_CERTS'):
        env['NODE_EXTRA_CA_CERTS'] = str(ca)
    allowed = () if no_tools else EDIT_TOOLS if edit else READ_TOOLS
    guard = home / 'guard.mjs'
    guard.write_text(_guard_source(allowed, provider, model_id), encoding='utf-8')
    context_window = settings.get('context_window', 128000)
    max_tokens = settings.get('max_tokens', 32768)
    if (type(context_window) is not int or type(max_tokens) is not int or
            max_tokens <= 0 or context_window <= max_tokens):
        raise DSHError('DSH context_window must exceed a positive max_tokens')
    route = {
        'api': 'openai-completions', 'baseURL': base_url, 'apiKeyEnv': 'OSS_DSH_API_KEY',
        'retryPolicy': {'mode': 'normal', 'maxRetries': 0},
        'models': [{'id': model_id, 'contextWindow': context_window, 'maxTokens': max_tokens}],
    }
    overlay = [{'id': name, 'disabled': True} for name in _DISABLED]
    overlay += [
        {'id': 'llm-pi-ai', 'config': {'providers': {provider: route}}},
        {'id': 'agent-default-model', 'config': {'provider': provider, 'model': model_id}},
        {'id': 'tool-fs', 'disabled': no_tools, 'config': {
            'read': True, 'readImage': False, 'edit': bool(edit), 'write': bool(edit), 'applyPatch': False}},
        {'id': 'tool-fs-search', 'disabled': no_tools},
        {'id': 'sandbox-policy', 'config': {
            'mode': 'workspace-write' if edit else 'read-only',
            'workspaceRoot': str(workspace), 'additionalWritableRoots': []}},
        {'id': 'approval', 'config': {'policy': 'never'}},
        {'id': 'permission', 'config': {'defaultPreset': 'workspace-write' if edit else 'read-only',
            'presets': {'read-only': {'sandbox': 'read-only', 'approval': 'never'},
                        'workspace-write': {'sandbox': 'workspace-write', 'approval': 'never'}}}},
        {'insert': [{'id': 'oss-dsh-guard', 'name': str(guard)}]},
        {'id': 'headless-runner', 'inject': ['headlessStartup', 'ossDshGuard']},
    ]
    patch = home / 'adapter.patch.json'
    patch.write_text(json.dumps(overlay, ensure_ascii=False, indent=2), encoding='utf-8')
    args += ['--profile', 'headless', '--patch', str(patch), '--json',
             '--approval-policy', 'never', '--permission-mode', env['DSH_PERMISSION_MODE'], '-']
    return {'args': args, 'env': env, 'stdin_text': prompt, 'allowed_tools': list(allowed),
            'dsh_home': str(home), 'config_path': str(patch)}


def parse_result(stdout, returncode, allowed_tools=()):
    """Require a complete native JSONL run, not merely exit zero or text."""
    if returncode:
        raise DSHError(f'DSH process failed (exit {returncode})')
    events = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            raise DSHProtocolError('DSH emitted malformed JSON events') from None
        if not isinstance(event, dict):
            raise DSHProtocolError('DSH emitted a non-object event')
        events.append(event)
    if not events or events[0].get('type') != 'session' or events[-1].get('type') != 'final':
        raise DSHProtocolError('DSH is missing its session or final completion event')
    session_id = events[0].get('sessionId')
    final = events[-1]
    if not isinstance(session_id, str) or not session_id or final.get('sessionId') != session_id:
        raise DSHProtocolError('DSH completion belongs to a different session')
    if not isinstance(final.get('reason'), dict) or final.get('truncated'):
        raise DSHProtocolError('DSH final event has no durable completion reason')
    if final['reason'].get('kind') != 'completed':
        raise DSHError('DSH did not report a completed turn')
    if not isinstance(final.get('text'), str) or not final['text'].strip():
        raise DSHProtocolError('DSH completed without a final answer')
    if re.search(r'<[^>\n]*DSML|<\|?(?:tool_calls?|function_calls?)\b', final['text'], re.IGNORECASE):
        raise DSHProtocolError('DSH final answer contains incomplete tool-control markup')
    allowed = set(allowed_tools)
    pending, calls, steps, turns = set(), set(), set(), []
    tool_errors = 0
    active_turn = active_step = None
    step_text, last_text = [], ''
    step_truncated = last_truncated = False
    for index, event in enumerate(events):
        if 'sessionId' in event and event['sessionId'] != session_id:
            raise DSHProtocolError('DSH event belongs to a different session')
        kind = event.get('type')
        if kind == 'session':
            if index != 0:
                raise DSHProtocolError('DSH emitted more than one session')
        elif kind == 'final':
            if index != len(events) - 1:
                raise DSHProtocolError('DSH emitted events after its final answer')
        elif kind in ('thinking', 'text'):
            if not isinstance(event.get('text'), str) or active_step is None:
                raise DSHProtocolError('DSH emitted invalid assistant content')
            if kind == 'text':
                step_text.append(event['text'])
                step_truncated |= bool(event.get('truncated'))
        elif kind == 'tool_call':
            call = event.get('callId')
            if (not isinstance(event.get('tool'), str) or event['tool'] not in allowed or
                    not isinstance(call, str) or not call or call in calls or active_step is None):
                raise DSHProtocolError('DSH emitted an unauthorized or invalid tool call')
            calls.add(call)
            pending.add(call)
        elif kind == 'tool_result':
            call = event.get('callId')
            status, error = event.get('status'), event.get('error')
            if not isinstance(call, str) or call not in pending or status not in ('completed', 'error'):
                raise DSHProtocolError('DSH emitted an unfinished or unmatched tool result')
            if error is not None and not isinstance(error, dict):
                raise DSHProtocolError('DSH emitted invalid tool error metadata')
            if status == 'completed' and error:
                raise DSHProtocolError('DSH emitted contradictory tool completion metadata')
            if status == 'error':
                # Native headless maps a durable isError tool result to `error`.
                # A completed read of a missing file is recoverable; aborts and
                # unknown tools are not evidence of finished work.
                error = error or {}
                if (error.get('code') in ('UNKNOWN_TOOL', 'ABORTED', 'ABORTED_BEFORE_DISPATCH') or
                        error.get('name') in ('ToolNotFoundError', 'AbortError', 'CanceledError', 'CancelledError')):
                    raise DSHProtocolError('DSH emitted an aborted or unknown tool result')
                tool_errors += 1
            pending.remove(call)
        elif kind == 'status':
            phase = event.get('phase')
            if phase not in ('turn_start', 'turn_end', 'step_start', 'step_end'):
                raise DSHProtocolError('DSH emitted an unknown run status')
            turn = event.get('turn')
            if type(turn) is not int or turn < 0:
                raise DSHProtocolError('DSH emitted an invalid turn identity')
            if phase == 'turn_start':
                if active_turn is not None or turn in turns:
                    raise DSHProtocolError('DSH emitted overlapping or repeated turns')
                active_turn = turn
            elif turn != active_turn:
                raise DSHProtocolError('DSH status has no matching active turn')
            if phase == 'step_start':
                step = event.get('step')
                if type(step) is not int or step < 0 or active_step is not None or (turn, step) in steps:
                    raise DSHProtocolError('DSH emitted an invalid or overlapping step')
                active_step = (turn, step)
                step_text, step_truncated = [], False
            if phase == 'step_end':
                key = (event.get('turn'), event.get('step'))
                if (any(type(value) is not int or value < 0 for value in key) or
                        key in steps or key != active_step or pending):
                    raise DSHProtocolError('DSH emitted duplicate or invalid completed steps')
                steps.add(key)
                active_step = None
                if ''.join(step_text):
                    last_text, last_truncated = ''.join(step_text), step_truncated
            if phase == 'turn_end':
                if not isinstance(event.get('reason'), dict) or event['reason'].get('kind') != 'completed':
                    raise DSHError('DSH emitted an interrupted or failed turn')
                if active_step is not None or pending:
                    raise DSHProtocolError('DSH ended a turn before its work completed')
                turns.append(event.get('turn'))
                active_turn = None
        else:
            raise DSHProtocolError('DSH emitted an error or unsupported event')
    if pending or not steps or not turns or active_turn is not None or active_step is not None:
        raise DSHProtocolError('DSH is missing completed tools, steps, or turns')
    if not last_text or not last_truncated and final['text'] != last_text:
        raise DSHProtocolError('DSH final answer does not match its committed assistant text')
    receipt = {'channel': 'dsh', 'session_id': session_id, 'finish_reason': 'completed',
               'completion_event': 'final', 'steps': len(steps), 'tool_calls': len(calls),
               'tool_errors': tool_errors, 'allowed_tools': sorted(allowed)}
    return {'text': final['text'], 'receipt': receipt,
            'usage': [event['usage'] for event in events if event.get('phase') == 'step_end' and 'usage' in event]}
