"""Run from the checkout, or set OSS_CONTRIBUTOR_ROOT when copying elsewhere."""
from pathlib import Path
import json
import os
import subprocess
import sys

def tick_command(root, python):
    config = json.loads((root / 'config.json').read_text(encoding='utf-8'))
    publish = config.get('automation', {}).get('scheduled_external_writes', False)
    if type(publish) is not bool:
        raise ValueError('automation.scheduled_external_writes must be a boolean')
    args = [python, '-X', 'utf8', str(root / 'contribute.py')]
    if publish:
        args.append('--allow-external-writes')
    return args + ['tick']


def main():
    root = Path(os.environ.get("OSS_CONTRIBUTOR_ROOT", Path(__file__).resolve().parent))
    python = os.environ.get("OSS_CONTRIBUTOR_PYTHON", sys.executable)
    args = tick_command(root, python)
    print('Scheduled publication: ' + ('enabled' if '--allow-external-writes' in args else 'disabled'), flush=True)
    result = subprocess.run(args, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.stdout.strip():
        print(result.stdout.strip())
    summaries = [line for line in result.stderr.splitlines() if line.startswith('PR maintenance gaps: ')]
    for line in summaries[:5]:
        print(line[:1000])
    if result.returncode:
        print(result.stderr.strip(), file=sys.stderr)
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
