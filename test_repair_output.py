import contextlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from autonomous import Autonomous, ModelOutputFormatError
from contribute import Pipeline
import test_autonomous as fixtures


def stream(text='Implemented the patch.', reason='stop', text_message='final', finish_message='final'):
    events = [{'type': 'text', 'part': {'messageID': text_message, 'text': text}}]
    if reason is not None:
        events.append({'type': 'step_finish', 'part': {'messageID': finish_message, 'reason': reason}})
    return '\n'.join(json.dumps(event) for event in events)


class RepairOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config.json').write_text(json.dumps({
            'repositories': ['owner/repo'], 'model': 'test/model', 'timeout_seconds': 30,
            'opencode_executable': 'opencode'}))
        self.p = Pipeline(self.root)
        self.addCleanup(self.p.db.close)
        self.engine = Autonomous(self.p)
        self.key = self.p.hunt('owner/repo', 'repair output')
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET workspace=? WHERE id=?', (str(self.root), self.key))
        self.data = {'stage': 'REPAIR', 'attempts': {}, 'plan': {'pr_body': 'Fix reported behavior'}}
        self.engine.store(self.key, self.data, 'REPAIR')

    def adapter(self, output, returncode=0, error=None):
        stack = contextlib.ExitStack()
        stack.enter_context(patch('autonomous.opencode_executable', return_value='opencode'))
        stack.enter_context(patch('autonomous.execute', side_effect=error,
                                  return_value=subprocess.CompletedProcess([], returncode, output, '')))
        return stack

    def test_completed_narrative_summary_is_audited_then_requires_verification(self):
        output = stream()
        with self.adapter(output):
            self.engine.repair(self.key, self.data)
        self.assertEqual('PATCH_READY', self.engine.load(self.key)['stage'])
        receipt = self.engine.load(self.key)['repair_output_format']
        self.assertEqual('final', receipt['message_id'])
        self.assertEqual('stop', receipt['finish_reason'])
        self.assertEqual('not_run', receipt['verification'])
        self.assertEqual('not_run', receipt['review'])
        self.assertEqual(output, Path(receipt['output_log']).read_text())
        self.assertEqual(receipt, json.loads((self.engine.packet_dir(self.key) / 'repair-output-format.json').read_text()))
        self.assertIsNone(self.p.db.execute("SELECT 1 FROM stages WHERE name IN ('testing','auto_review','pr_publication')").fetchone())

    def test_incomplete_or_ambiguous_completion_is_never_recovered(self):
        cases = {'missing_stop': stream(reason=None), 'tool_calls': stream(reason='tool-calls'),
                 'length': stream(reason='length'), 'different_message': stream(finish_message='other'),
                 'missing_message': stream(text_message='', finish_message=''),
                 'dsml': stream(text='<｜DSML｜function_calls><｜DSML｜invoke name="edit">'),
                 'malformed_stream': stream() + '\nnot a stream event'}
        for name, output in cases.items():
            with self.subTest(case=name), self.adapter(output):
                with self.assertRaises(ValueError) as raised:
                    self.engine.repair(self.key, self.data)
                self.assertNotIsInstance(raised.exception, ModelOutputFormatError)
            self.assertEqual('REPAIR', self.engine.load(self.key)['stage'])
        self.assertFalse((self.engine.packet_dir(self.key) / 'repair-output-format.json').exists())

    def test_provider_failure_and_timeout_are_not_optional_summary_errors(self):
        provider_error = json.dumps({'type': 'error', 'error': {'data': {'message': 'provider failed'}}})
        for name, output, returncode, error in [
                ('nonzero', stream(), 1, None), ('error_event', provider_error + '\n' + stream(), 0, None),
                ('timeout', '', 0, subprocess.TimeoutExpired('opencode', 1))]:
            with self.subTest(case=name), self.adapter(output, returncode, error):
                with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
                    self.engine.repair(self.key, self.data)
            self.assertEqual('REPAIR', self.engine.load(self.key)['stage'])

    def test_planning_and_feedback_keep_strict_schema_parsing(self):
        for purpose in ('plan', 'feedback', 'research'):
            with self.subTest(purpose=purpose), self.adapter(stream()):
                with self.assertRaises(json.JSONDecodeError):
                    self.engine.model(self.key, purpose, 'Return JSON')

    def test_valid_summary_keeps_normal_result_without_recovery_record(self):
        with self.adapter(stream(text='{"summary":"Implemented patch"}')):
            answer = self.engine.model(self.key, 'repair', 'Return JSON', edit=True)
        self.assertEqual({'summary': 'Implemented patch'}, answer)
        self.assertNotIn('repair_output_format', self.engine.load(self.key))


class RepairOutputVerificationTests(unittest.TestCase):
    tearDown = fixtures.AutonomousTests.tearDown
    git = fixtures.AutonomousTests.git

    def setUp(self):
        fixtures.AutonomousTests.setUp(self)
        original = subprocess.Popen
        def local_only(args, *positional, **kwargs):
            if isinstance(args, str) or kwargs.get('shell') or Path(str(args[0])).stem.lower() == 'gh':
                raise AssertionError('This verification test permits local commands only')
            return original(args, *positional, **kwargs)
        guard = patch('subprocess.Popen', side_effect=local_only)
        guard.start()
        self.addCleanup(guard.stop)

    def test_recovered_summary_still_runs_real_red_green_and_review_gate(self):
        self.p.config.update(model='test/model', opencode_executable='opencode')
        fixtures.AutonomousTests.model(self, self.key, 'plan', '', edit=True)
        (self.work / 'value.py').write_text('VALUE = 2\n')
        data = {'stage': 'REPAIR', 'attempts': {}, 'plan': self.plan}
        self.engine.store(self.key, data, 'REPAIR')
        with patch('autonomous.opencode_executable', return_value='opencode'), \
                patch('autonomous.gh', side_effect=lambda *args: fixtures.AutonomousTests.fake_gh(self, *args)), \
                patch('autonomous.execute', return_value=subprocess.CompletedProcess([], 0, stream(), '')):
            self.engine.repair(self.key, data)
        self.engine.verify(self.key, data)
        self.assertEqual('AUTO_REVIEW', self.engine.load(self.key)['stage'])
        evidence = json.loads((self.engine.packet_dir(self.key) / 'verification.json').read_text())
        self.assertTrue(evidence['passed'])
        self.assertEqual([1, 0, 0], [item['exit_code'] for item in evidence['results']])
        with patch.object(self.engine, 'judge', return_value={'approved': False, 'reason': 'unaddressed edge case'}):
            with self.assertRaisesRegex(RuntimeError, 'Patch review rejected'):
                self.engine.review(self.key, data)
        self.assertEqual('AUTO_REVIEW', self.engine.load(self.key)['stage'])
        self.assertNotIn('approved_fingerprint', self.engine.load(self.key))
        self.assertEqual([], self.created_prs)


if __name__ == '__main__':
    unittest.main()
