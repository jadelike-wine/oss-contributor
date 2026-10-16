"""Run from the checkout, or set OSS_CONTRIBUTOR_ROOT when copying elsewhere."""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(os.environ.get("OSS_CONTRIBUTOR_ROOT", Path(__file__).resolve().parent))
PYTHON = os.environ.get("OSS_CONTRIBUTOR_PYTHON", sys.executable)
result = subprocess.run([PYTHON, "-X", "utf8", str(ROOT / "contribute.py"), "tick", "--analyze"],
                        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
if result.stdout.strip():
    print(result.stdout.strip())
if result.returncode:
    print(result.stderr.strip(), file=sys.stderr)
sys.exit(result.returncode)
