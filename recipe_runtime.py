"""Validate finite setup recipes and identify meaningful environment changes.

This module only inspects local files. It never installs packages or starts a
server while checking a recipe.
"""
import hashlib
from functools import lru_cache
import json
import os
from pathlib import Path
import platform
import re
import shutil
import shlex


class SetupError(RuntimeError):
    def __init__(self, message, context=None, domain='setup_error'):
        super().__init__('Setup recipe failed; not a candidate defect: ' + message)
        self.context = context or {}
        self.domain = domain


@lru_cache(maxsize=64)
def _content_digest(path, size, modified):
    # Stat values invalidate the cache, but only actual bytes enter identity.
    digest = hashlib.sha256()
    with open(path, 'rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _file(path):
    path = Path(path)
    if not path.is_file():
        return {'path': str(path), 'exists': False}
    stat = path.stat()
    return {'path': str(path.resolve()), 'exists': True, 'bytes': stat.st_size,
            'sha256': _content_digest(str(path.resolve()), stat.st_size, stat.st_mtime_ns)}


def _runtime_files(argv, work, which):
    executable = which(argv[0]) if not Path(argv[0]).is_absolute() else argv[0]
    executable = executable or str(work / argv[0])
    paths = [executable]
    if argv[0] == 'bun':
        local_bun = Path(__file__).resolve().parent / 'state/tooling/node_modules/bun/bin' / ('bun.exe' if os.name == 'nt' else 'bun')
        if local_bun.is_file():
            paths = [str(local_bun)]
    if Path(argv[0]).stem.lower() in ('node', 'nodejs'):
        # A runtime's first positional argument is its entry file. Inline code
        # and eval modes intentionally do not have one.
        if len(argv) > 1 and not argv[1].startswith('-'):
            paths.append(str((work / argv[1]).resolve()))
    if os.name == 'nt' and Path(argv[0]).stem.lower() in ('npm', 'pnpm', 'yarn', 'corepack'):
        from delivery import node_command
        try:
            paths = node_command(argv[0], which)
        except ValueError:
            pass
    return [_file(path) for path in paths]


def setup_context(spec, workspace, which=shutil.which):
    """Only setup/runtime/manifests affect retry identity; test edits do not."""
    work = Path(workspace).resolve()
    manifests = []
    directories = {work, *(_command_directory(argv, work)[1] for argv in spec.get('setup', []))}
    for directory in sorted(directories):
        for name in ('package.json', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock',
                     'pnpm-workspace.yaml', '.nvmrc', '.node-version', '.yarnrc.yml', '.npmrc',
                     'pyproject.toml', 'uv.lock', 'requirements.txt', '.gitmodules'):
            path = directory / name
            if path.is_file():
                label = str(path.relative_to(work)) if path.is_relative_to(work) else str(path)
                manifests.append({'name': label, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    tools = [_runtime_files(argv, work, which) for argv in spec.get('setup', [])]
    value = {'setup': spec.get('setup', []), 'manifests': manifests, 'tools': tools,
             'platform': platform.system(), 'machine': platform.machine()}
    value['fingerprint'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value


def _script_commands(text):
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=';&|')
        lexer.whitespace_split = True
        commands, current = [], []
        for token in lexer:
            if token and all(char in ';&|' for char in token):
                if current:
                    commands.append(current)
                current = []
            else:
                current.append(token)
        return commands + ([current] if current else [])
    except ValueError:
        return []


def _server_command(command):
    """Inspect executable/subcommand positions, never package names or prose."""
    if isinstance(command, str):
        return any(_server_command(args) for args in _script_commands(command))
    args = list(command)
    if not args:
        return False
    if Path(args[0]).stem.lower() in ('cross-env', 'cross-env-shell', 'env'):
        args = args[1:]
    while args and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', args[0]):
        args = args[1:]
    if not args:
        return False
    name = Path(args[0]).stem.lower()
    if name in ('echo', 'printf'):
        return False
    if name in ('npm', 'npm-cli', 'pnpm', 'yarn', 'bun', 'npx'):
        tokens = args[1:]
        if name == 'npx':
            return _server_command(tokens)
        if not tokens or tokens[0] in ('install', 'add', 'ci', 'update', 'upgrade', 'remove', 'uninstall', 'run', 'run-script'):
            return False
        if tokens[0] in ('exec', 'dlx'):
            tokens = tokens[1:]
            if tokens and tokens[0] == '--':
                tokens = tokens[1:]
        return _server_command(tokens)
    if any(token in ('--help', '--version', '-h') for token in args[1:]):
        return False
    if any(token == '--watch' and (i + 1 >= len(args) or args[i + 1] not in ('false', '0'))
           or token.startswith('--watch=') and token.split('=', 1)[1] not in ('false', '0')
           for i, token in enumerate(args)):
        return True
    subcommand = next((token for token in args[1:] if not token.startswith('-')), '')
    if name in ('start-storybook', 'http-server', 'webpack-dev-server'):
        return True
    if name == 'storybook':
        return subcommand == 'dev' or not subcommand or any(token in ('-p', '--port', '--host') for token in args[1:])
    if name == 'vite':
        return not subcommand or subcommand in ('dev', 'serve', 'preview')
    return name in ('next', 'nuxt', 'astro', 'webpack') and subcommand in ('dev', 'start', 'serve')


def _command_directory(argv, workspace):
    work, args, index = Path(workspace).resolve(), [], 0
    while index < len(argv):
        token = argv[index]
        if token in ('--cwd', '-C', '--dir', '--prefix') and index + 1 < len(argv):
            work = (Path(workspace) / argv[index + 1]).resolve()
            index += 2
            continue
        if any(token.startswith(flag + '=') for flag in ('--cwd', '--dir', '--prefix')):
            work = (Path(workspace) / token.split('=', 1)[1]).resolve()
        else:
            args.append(token)
        index += 1
    return args, work


def _setup_command(argv, workspace, depth=0):
    args = list(argv)
    if Path(args[0]).stem.lower() in ('node', 'nodejs') and len(args) > 1:
        args = args[1:]
    if Path(args[0]).stem.lower() == 'corepack' and len(args) > 1 and args[1] in ('npm', 'pnpm', 'yarn'):
        args = args[1:]
    args, work = _command_directory(args, workspace)
    package = work / 'package.json'
    try:
        scripts = json.loads(package.read_text(encoding='utf-8')).get('scripts', {})
    except (ValueError, OSError):
        scripts = {}
    manager = Path(args[0]).stem.lower()
    if manager in ('npm', 'npm-cli', 'pnpm', 'yarn', 'bun'):
        tokens = args[1:]
        if tokens and tokens[0] in ('run', 'run-script'):
            tokens = tokens[1:]
        if tokens and isinstance(scripts.get(tokens[0]), str):
            script = scripts[tokens[0]]
            if _server_command(script):
                return script
            if depth < 4:
                for nested in _script_commands(script):
                    if nested:
                        server = _setup_command(nested, work, depth + 1)
                        if server:
                            return server
            return None
    return ' '.join(args) if _server_command(args) else None


def validate_setup(spec, workspace, which=shutil.which, step=None):
    context = setup_context(spec, workspace, which)
    for index, argv in enumerate(spec.get('setup', [])):
        service = _setup_command(argv, workspace)
        if service:
            raise SetupError('Foreground service in setup: ' + service +
                             '. Use the test runner webServer lifecycle or a finite test command.', context)
        files = _runtime_files(argv, Path(workspace), which)
        if index != (step if step is not None else 0):
            continue  # Earlier steps can create venvs, installed tools or built entry files.
        for item in files:
            if not item['exists']:
                raise SetupError('Missing executable or runtime entry file: ' + item['path'], context)
    return context
