import contextlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from contribute import Pipeline


def native_stream(text='静态分析报告；尚未运行测试。', reason='completed'):
    events = [
        {'type': 'session', 'sessionId': 'legacy-isolated'},
        {'type': 'status', 'phase': 'turn_start', 'turn': 1},
        {'type': 'status', 'phase': 'step_start', 'turn': 1, 'step': 1},
        {'type': 'text', 'text': text},
        {'type': 'status', 'phase': 'step_end', 'turn': 1, 'step': 1,
         'usage': {'inputTokens': 10, 'outputTokens': 7, 'cacheReadTokens': 4}},
        {'type': 'status', 'phase': 'turn_end', 'turn': 1, 'reason': {'kind': reason}},
        {'type': 'final', 'sessionId': 'legacy-isolated', 'reason': {'kind': reason}, 'text': text},
    ]
    return '\n'.join(json.dumps(event, ensure_ascii=False) for event in events)


class LegacyDshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config.json').write_text(json.dumps({
            'repositories': ['owner/repo'], 'implementation_channel': 'dsh',
            'model': 'tokenking/devin/swe-2', 'research': {'model': 'tokenking/deepseek-v4.1-flash'},
            'timeout_seconds': 30, 'timeouts': {'model_seconds': 12}, 'opencode_executable': 'opencode'}))
        self.pipeline = Pipeline(self.root)
        self.addCleanup(self.pipeline.db.close)
        self.key = self.pipeline.hunt('owner/repo', 'legacy existing issue')
        self.work = self.root / 'work'
        self.work.mkdir()
        self.folder = self.root / 'runs' / self.key
        self.folder.mkdir(parents=True)
        (self.folder / 'analysis.md').write_text('Previous static analysis; testing is still required.')
        with self.pipeline.db:
            self.pipeline.db.execute(
                "UPDATE tasks SET source='issue',number=1,state='NEEDS_REPRODUCTION',workspace=? WHERE id=?",
                (str(self.work), self.key))

    def base_mocks(self):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(self.pipeline, 'require_contribution'))
        stack.enter_context(patch.object(self.pipeline, 'prepare', return_value=str(self.work)))
        stack.enter_context(patch.object(self.pipeline, 'report'))
        stack.enter_context(patch('contribute.gh', return_value={'state': 'OPEN', 'title': 'Bug'}))
        return stack

    def invocation(self, config, root, work, prompt, *, model, edit):
        self.invoked = {'model': model, 'edit': edit, 'prompt': prompt.read_text(encoding='utf-8')}
        return {'args': ['dsh-cli', '--headless'], 'env': {'TEST_ONLY': '1'},
                'stdin_text': self.invoked['prompt'],
                'allowed_tools': ('read', 'glob', 'grep', 'edit', 'write') if edit else ('read', 'glob', 'grep')}

    def execute(self, args, **kwargs):
        self.executed = {'args': args, **kwargs}
        kwargs['started'](424242)
        self.child = self.pipeline.db.execute("SELECT child_pid FROM locks WHERE name='worker'").fetchone()[0]
        output = native_stream()
        Path(str(kwargs['output_prefix']) + '.jsonl').write_text(output, encoding='utf-8')
        kwargs['started'](None)
        return subprocess.CompletedProcess(args, 0, output, '')

    def test_legacy_analyze_uses_dsh_free_text_and_records_actual_model_usage(self):
        with self.base_mocks(), patch('dsh_adapter.build_invocation', side_effect=self.invocation), \
                patch('contribute.execute', side_effect=self.execute), \
                patch('contribute.opencode_executable', side_effect=AssertionError('OpenCode must not run')):
            report = Path(self.pipeline.analyze(self.key))
        self.assertEqual(report.read_text(encoding='utf-8'), '静态分析报告；尚未运行测试。')
        self.assertFalse(self.invoked['edit'])
        self.assertEqual(self.invoked['model'], 'tokenking/devin/swe-2')
        self.assertEqual(self.child, 424242)
        self.assertEqual(self.executed['cwd'], str(self.work))
        self.assertEqual(self.executed['stdin_text'], self.invoked['prompt'])
        self.assertNotIn(self.invoked['prompt'], self.executed['args'])
        self.assertLessEqual(self.executed['timeout'], 12)
        row = self.pipeline.db.execute('SELECT * FROM model_calls').fetchone()
        self.assertEqual((row['channel'], row['model'], row['outcome']),
                         ('dsh', 'tokenking/devin/swe-2', 'success'))
        self.assertEqual((row['input_tokens'], row['output_tokens']), (14, 7))
        self.assertIsNotNone(row['ended'])
        self.assertEqual(self.pipeline.task(self.key)['state'], 'NEEDS_REPRODUCTION')
        self.assertEqual(self.pipeline.db.execute('SELECT count(*) FROM locks').fetchone()[0], 0)
        receipt = json.loads((self.folder / 'analyze-execution.json').read_text())
        self.assertEqual(receipt['session_id'], 'legacy-isolated')
        self.assertTrue(Path(receipt['output_log']).is_file())

    def test_legacy_repair_requests_edit_tools_but_still_requires_verification(self):
        with self.base_mocks(), patch('dsh_adapter.build_invocation', side_effect=self.invocation), \
                patch('contribute.execute', side_effect=self.execute):
            report = Path(self.pipeline.analyze(self.key, mode='repair'))
        self.assertTrue(self.invoked['edit'])
        self.assertIn('Prior analysis:', self.invoked['prompt'])
        self.assertEqual(report.name, 'repair.md')
        self.assertEqual(self.pipeline.task(self.key)['state'], 'PATCH_READY')
        self.assertEqual(self.pipeline.db.execute('SELECT purpose FROM model_calls').fetchone()[0], 'repair')

    def test_timeout_records_failure_and_releases_worker(self):
        def timeout(args, **kwargs):
            kwargs['started'](424242)
            raise subprocess.TimeoutExpired('dsh-cli', kwargs['timeout'])
        with self.base_mocks(), patch('dsh_adapter.build_invocation', side_effect=self.invocation), \
                patch('contribute.execute', side_effect=timeout), self.assertRaises(subprocess.TimeoutExpired):
            self.pipeline.analyze(self.key)
        row = self.pipeline.db.execute('SELECT * FROM model_calls').fetchone()
        self.assertEqual((row['channel'], row['outcome']), ('dsh', 'timeout'))
        self.assertIsNotNone(row['ended'])
        self.assertEqual(self.pipeline.task(self.key)['state'], 'ANALYSIS_FAILED')
        self.assertEqual(self.pipeline.db.execute('SELECT count(*) FROM locks').fetchone()[0], 0)

    def test_incomplete_dsh_turn_does_not_replace_report_or_mark_patch_ready(self):
        before = (self.folder / 'analysis.md').read_text()
        result = subprocess.CompletedProcess([], 0, native_stream(reason='cancelled'), '')
        with self.base_mocks(), patch('dsh_adapter.build_invocation', side_effect=self.invocation), \
                patch('contribute.execute', return_value=result), self.assertRaises(RuntimeError):
            self.pipeline.analyze(self.key)
        row = self.pipeline.db.execute('SELECT * FROM model_calls').fetchone()
        self.assertNotEqual(row['outcome'], 'success')
        self.assertIsNotNone(row['ended'])
        self.assertEqual((self.folder / 'analysis.md').read_text(), before)
        self.assertEqual(self.pipeline.task(self.key)['state'], 'ANALYSIS_FAILED')

    def test_old_config_keeps_opencode_and_finishes_its_usage_record(self):
        self.pipeline.config.pop('implementation_channel')
        def launch(args, **kwargs):
            self.legacy_args, self.legacy_env = args, kwargs['env']
            events = [{'type': 'text', 'part': {'text': 'Original OpenCode report'}},
                      {'type': 'step_finish', 'part': {'id': 'step',
                       'tokens': {'input': 9, 'output': 4, 'cache': {'read': 2, 'write': 0}}}}]
            kwargs['stdout'].write('\n'.join(json.dumps(event) for event in events))
            process = Mock(pid=424242)
            process.wait.return_value = 0
            return process
        with self.base_mocks(), patch('contribute.opencode_executable', return_value='opencode'), \
                patch('contribute.subprocess.Popen', side_effect=launch), \
                patch('dsh_adapter.build_invocation', side_effect=AssertionError('DSH must not run')):
            report = Path(self.pipeline.analyze(self.key))
        self.assertEqual(report.read_text(), 'Original OpenCode report')
        self.assertEqual(self.legacy_args[0], 'opencode')
        self.assertEqual(self.legacy_env['OPENCODE_DISABLE_PROJECT_CONFIG'], '1')
        row = self.pipeline.db.execute('SELECT * FROM model_calls').fetchone()
        self.assertEqual((row['channel'], row['outcome']), ('opencode', 'success'))
        self.assertEqual((row['input_tokens'], row['output_tokens']), (11, 4))
        self.assertIsNotNone(row['ended'])


if __name__ == '__main__':
    unittest.main()
