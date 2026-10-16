import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autonomous import runtime_tools
from contribute import execute


class RuntimeTests(unittest.TestCase):
    def test_timeout_retains_model_output_and_clears_child(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / 'model'
            tracked = []
            with self.assertRaises(subprocess.TimeoutExpired):
                execute([sys.executable, '-u', '-c',
                         'import time; print("partial response", flush=True); time.sleep(30)'],
                        timeout=2, output_prefix=prefix, started=tracked.append)
            self.assertIn('partial response', Path(str(prefix) + '.jsonl').read_text())
            self.assertIsInstance(tracked[0], int)
            self.assertIsNone(tracked[-1])

    def test_output_capture_keeps_stdout_and_stderr_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            result = execute([sys.executable, '-c',
                              'import sys; print("result"); print("diagnostic", file=sys.stderr)'],
                             output_prefix=Path(directory) / 'model')
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), 'result')
            self.assertEqual(result.stderr.strip(), 'diagnostic')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows launcher resolution')
    def test_windows_shims_resolve_to_existing_runtime_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'node_modules/npm/bin/npm-cli.js'
            script.parent.mkdir(parents=True)
            script.write_text('')
            found = {'node': str(root / 'node.exe'), 'npm': str(root / 'npm.cmd'),
                     'pnpm': str(root / 'pnpm.cmd'), 'mvn': str(root / 'mvn.cmd')}
            with patch('autonomous.shutil.which', side_effect=found.get):
                tools = runtime_tools()
            self.assertEqual(tools['npm'], [found['node'], str(script)])
            self.assertIsNone(tools['pnpm'])
            self.assertIsNone(tools['mvn'])


if __name__ == '__main__':
    unittest.main()
