import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from autonomous import json_answer, runtime_tools, validate_spec
from contribute import execute, save


class RuntimeTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'Windows checkpoint sharing')
    def test_atomic_save_recovers_after_real_windows_reader_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            save(path, {'version': 1})
            with path.open(encoding='utf-8') as reader:
                release = threading.Timer(0.15, reader.close)
                release.start()
                try:
                    save(path, {'version': 2})
                finally:
                    release.cancel()
                    release.join()
            self.assertEqual(json.loads(path.read_text()), {'version': 2})

    def test_atomic_save_retries_windows_sharing_violation(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            save(path, {'version': 1})
            replace = os.replace
            blocked = PermissionError('checkpoint is open in a reader')
            blocked.winerror = 32
            calls = []
            def once(source, target):
                calls.append(source)
                if len(calls) == 1:
                    self.assertEqual(json.loads(path.read_text()), {'version': 1})
                    raise blocked
                replace(source, target)
            with patch('contribute.os.replace', side_effect=once), patch('time.sleep') as sleep:
                save(path, {'version': 2})
            self.assertEqual(json.loads(path.read_text()), {'version': 2})
            self.assertEqual(len(calls), 2)
            sleep.assert_called_once()
            self.assertEqual(list(path.parent.glob('*.tmp')), [])

    def test_atomic_save_preserves_predecessor_when_access_stays_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            save(path, {'version': 1})
            blocked = PermissionError('access denied')
            blocked.winerror = 5
            with patch('contribute.os.replace', side_effect=blocked) as replace, patch('time.sleep'):
                with self.assertRaises(PermissionError):
                    save(path, {'version': 2})
            self.assertLessEqual(replace.call_count, 5)
            self.assertEqual(json.loads(path.read_text()), {'version': 1})
            self.assertEqual(list(path.parent.glob('*.tmp')), [])

    @unittest.skipUnless(sys.platform == 'win32', 'Windows launcher resolution')
    def test_bare_corepack_runs_through_node_without_shell(self):
        import shutil
        if not shutil.which('corepack'):
            self.skipTest('Corepack is not installed')
        result = execute(['corepack', '--version'])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertRegex(result.stdout.strip(), r'^\d+\.\d+\.\d+')

    def test_bun_run_cwd_order_cannot_silently_skip_check(self):
        spec = {'setup': [], 'regression': ['bun', 'test', 'test/a.test.ts'],
                'checks': [['bun', '--cwd', 'packages/ai', 'run', 'typecheck']],
                'test_files': ['test/a.test.ts'], 'baseline_failure_pattern': 'specific assertion'}
        with self.assertRaisesRegex(ValueError, 'print help'):
            validate_spec(spec)
        spec['checks'] = [['bun', 'run', '--cwd', 'packages/ai', 'typecheck']]
        self.assertEqual(validate_spec(spec), spec)

    def test_single_fenced_answer_after_explanation(self):
        self.assertEqual(json_answer('Explanation.\n```json\n{"approved":false,"reason":"failed"}\n```'),
                         {'approved': False, 'reason': 'failed'})
        with self.assertRaises(ValueError):
            json_answer('```json\n{"approved":false}\n```\n```json\n{"approved":true}\n```')

    def test_trailing_object_after_explanation_is_unambiguous(self):
        self.assertEqual(json_answer('Applied the patch.\n\n{"summary":"done"}'), {'summary': 'done'})
        for text in ('Note\n{"approved":false}\n{"approved":true}', 'Note\n{"approved":true}\nmore text'):
            with self.assertRaises(ValueError):
                json_answer(text)

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
