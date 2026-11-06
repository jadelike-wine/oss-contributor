"""DSH completion proofs and isolated least-capability launch preparation."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dsh_adapter import (DSHError, DSHProtocolError, EDIT_TOOLS, READ_TOOLS,
                         build_invocation, discover_cli, parse_result)


def completed_events():
    return [
        {'type': 'session', 'sessionId': 'session-fixture', 'cwd': '/workspace'},
        {'type': 'status', 'phase': 'turn_start', 'turn': 1},
        {'type': 'status', 'phase': 'step_start', 'turn': 1, 'step': 1},
        {'type': 'text', 'text': '{"ok": true}'},
        {'type': 'status', 'phase': 'step_end', 'turn': 1, 'step': 1,
         'usage': {'inputTokens': 90, 'outputTokens': 20, 'cacheReadTokens': 10, 'totalTokens': 120}},
        {'type': 'status', 'phase': 'turn_end', 'turn': 1, 'reason': {'kind': 'completed'}},
        {'type': 'final', 'sessionId': 'session-fixture', 'reason': {'kind': 'completed'},
         'text': '{"ok": true}'},
    ]


def stream(events):
    return '\n'.join(json.dumps(event) for event in events)


class DSHCompletionTests(unittest.TestCase):
    def test_native_completion_and_usage_are_preserved(self):
        result = parse_result(stream(completed_events()), 0)
        self.assertEqual(result['text'], '{"ok": true}')
        self.assertEqual(result['receipt']['session_id'], 'session-fixture')
        self.assertEqual(result['receipt']['finish_reason'], 'completed')
        self.assertEqual(result['usage'][0]['inputTokens'], 90)

    def test_exit_zero_alone_and_text_alone_are_not_completion(self):
        for events in ([], [{'type': 'text', 'text': '{}'}], completed_events()[:-1]):
            with self.subTest(events=events), self.assertRaises(DSHProtocolError):
                parse_result(stream(events), 0)

    def test_process_failure_is_a_runtime_error(self):
        with self.assertRaises(DSHError):
            parse_result(stream(completed_events()), 1)

    def test_malformed_nonobject_extra_session_and_error_events_rejected(self):
        for extra in ('not JSON', '[]', json.dumps(completed_events()[0]),
                      '{"type":"error"}', '{"type":"interrupted"}'):
            raw = stream(completed_events()[:-1]) + '\n' + extra + '\n' + stream(completed_events()[-1:])
            with self.subTest(extra=extra), self.assertRaises(DSHProtocolError):
                parse_result(raw, 0)

    def test_all_session_identity_fields_must_match(self):
        for index in (2, 4, 6):
            events = completed_events()
            events[index]['sessionId'] = 'another-session'
            with self.subTest(index=index), self.assertRaises(DSHProtocolError):
                parse_result(stream(events), 0)

    def test_null_missing_truncated_or_duplicate_final_is_not_proof(self):
        for change in ({'reason': None}, {'reason': {}}, {'truncated': True}, {'text': ''}):
            events = completed_events()
            events[-1].update(change)
            with self.subTest(change=change), self.assertRaises((DSHProtocolError, DSHError)):
                parse_result(stream(events), 0)
        events = completed_events()
        events.append(events[-1])
        with self.assertRaises(DSHProtocolError):
            parse_result(stream(events), 0)

    def test_interruption_in_earlier_turn_cannot_be_hidden_by_final(self):
        events = completed_events()
        events[-2]['reason'] = {'kind': 'interrupted'}
        with self.assertRaises(DSHError):
            parse_result(stream(events), 0)

    def test_completed_known_tools_require_matching_terminal_results(self):
        tool = {'type': 'tool_call', 'callId': 'c1', 'tool': 'edit', 'input': {}}
        result = {'type': 'tool_result', 'callId': 'c1', 'status': 'completed', 'result': 'changed'}
        events = completed_events()
        events[3:3] = [tool, result]
        self.assertEqual(parse_result(stream(events), 0, EDIT_TOOLS)['receipt']['tool_calls'], 1)
        for names, altered in ((READ_TOOLS, events), (EDIT_TOOLS, events[:4] + events[5:])):
            with self.assertRaises(DSHProtocolError):
                parse_result(stream(altered), 0, names)
        for field in ({'status': 'pending'}, {'error': {'code': 'DENIED'}}, {'callId': 'unknown'}):
            broken = json.loads(json.dumps(events))
            broken[4].update(field)
            with self.subTest(field=field), self.assertRaises(DSHProtocolError):
                parse_result(stream(broken), 0, EDIT_TOOLS)

    def test_allowed_read_error_then_corrected_read_can_complete(self):
        base = completed_events()
        events = base[:3] + [
            {'type': 'tool_call', 'callId': 'missing', 'tool': 'read', 'input': {'file_path': 'missing.txt'}},
            {'type': 'tool_result', 'callId': 'missing', 'status': 'error',
             'result': 'File does not exist', 'error': {'name': 'Error', 'code': 'ENOENT'}},
            base[4],
            {'type': 'status', 'phase': 'step_start', 'turn': 1, 'step': 2},
            {'type': 'tool_call', 'callId': 'corrected', 'tool': 'read', 'input': {'file_path': 'exists.txt'}},
            {'type': 'tool_result', 'callId': 'corrected', 'status': 'completed', 'result': 'File content'},
            base[3],
            {**base[4], 'step': 2},
        ] + base[-2:]
        receipt = parse_result(stream(events), 0, READ_TOOLS)['receipt']
        self.assertEqual(receipt['tool_errors'], 1)
        self.assertEqual(receipt['tool_calls'], 2)
        self.assertEqual(receipt['steps'], 2)

    def test_nonterminal_unknown_and_aborted_tool_results_remain_rejected(self):
        for status, error in (
                ('pending', None), ('cancelled', None), ('canceled', None),
                ('running', None), ('unfinished', None), ('unknown', None), (None, None),
                ('error', {'code': 'ABORTED'}), ('error', {'code': 'ABORTED_BEFORE_DISPATCH'}),
                ('error', {'code': 'UNKNOWN_TOOL'}), ('error', {'name': 'AbortError'})):
            events = completed_events()
            events[3:3] = [
                {'type': 'tool_call', 'callId': 'c1', 'tool': 'read', 'input': {}},
                {'type': 'tool_result', 'callId': 'c1', 'status': status, 'error': error},
            ]
            with self.subTest(status=status, error=error), self.assertRaises(DSHProtocolError):
                parse_result(stream(events), 0, READ_TOOLS)

    def test_recoverable_tool_error_cannot_replace_final_completion(self):
        events = completed_events()[:-1]
        events[3:3] = [
            {'type': 'tool_call', 'callId': 'c1', 'tool': 'read', 'input': {}},
            {'type': 'tool_result', 'callId': 'c1', 'status': 'error', 'result': 'File missing'},
        ]
        with self.assertRaises(DSHProtocolError):
            parse_result(stream(events), 0, READ_TOOLS)

    def test_control_markup_is_not_repair_narrative(self):
        for text in ('<｜DSML｜invoke name="bash">', '<tool_call>bad</tool_call>', '<|function_calls>'):
            events = completed_events()
            events[-1]['text'] = text
            with self.subTest(text=text), self.assertRaises(DSHProtocolError):
                parse_result(stream(events), 0)

    def test_duplicate_invalid_or_missing_completed_steps_rejected(self):
        events = completed_events()
        for altered in (events[:4] + events[5:], events[:5] + [events[4]] + events[5:]):
            with self.assertRaises(DSHProtocolError):
                parse_result(stream(altered), 0)
        for value in (True, '1', -1, None):
            events = completed_events()
            events[4]['step'] = value
            with self.subTest(value=value), self.assertRaises(DSHProtocolError):
                parse_result(stream(events), 0)

    def test_unclosed_or_unstarted_steps_and_turns_rejected(self):
        for missing in (1, 2, 4, 5):
            events = completed_events()
            del events[missing]
            with self.subTest(missing=missing), self.assertRaises(DSHProtocolError):
                parse_result(stream(events), 0)

    def test_final_text_must_match_committed_text_unless_projection_truncated(self):
        events = completed_events()
        events[-1]['text'] = '{"different": true}'
        with self.assertRaises(DSHProtocolError):
            parse_result(stream(events), 0)
        events[3] = {'type': 'text', 'text': '{"different":', 'truncated': True}
        self.assertEqual(parse_result(stream(events), 0)['text'], '{"different": true}')


class DSHInvocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='oss-dsh-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.prompt = self.root / 'prompt.txt'
        self.prompt.write_text('A task with spaces and Unicode 中文.', encoding='utf8')
        self.config = {'dsh': {'base_url': 'https://api.example.invalid/v1', 'api_key_env': 'FIXTURE_DSH_KEY'}}
        self.env = patch.dict(os.environ, {'FIXTURE_DSH_KEY': 'fixture-key', 'DSH_HOME': 'desktop-profile',
                                          'DSH_PERMISSION_MODE': 'danger-full-access', 'NODE_OPTIONS': 'unsafe'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cli = patch('dsh_adapter.discover_cli', return_value=(['dsh-native'], {}))
        self.cli.start()
        self.addCleanup(self.cli.stop)

    def build(self, **kwargs):
        return build_invocation(self.config, self.root, self.workspace, self.prompt,
                                model='tokenking/devin/swe-2', **kwargs)

    def test_isolated_homes_credentials_only_in_env_and_prompt_only_in_stdin(self):
        first, second = self.build(), self.build()
        self.assertNotEqual(first['dsh_home'], second['dsh_home'])
        self.assertTrue(Path(first['dsh_home']).is_relative_to(self.root / 'state/dsh'))
        self.assertEqual(first['stdin_text'], self.prompt.read_text(encoding='utf8'))
        self.assertNotIn(first['stdin_text'], first['args'])
        self.assertEqual(first['env']['OSS_DSH_API_KEY'], 'fixture-key')
        self.assertNotIn('NODE_OPTIONS', first['env'])
        self.assertEqual(first['env']['DSH_PERMISSION_MODE'], 'read-only')
        self.assertEqual(first['env']['DSH_TELEMETRY_MODE'], 'DISABLED')
        files = list(Path(first['dsh_home']).iterdir())
        self.assertFalse(any('fixture-key' in path.read_text(encoding='utf8') for path in files))
        self.assertNotIn('fixture-key', ' '.join(first['args']))

    def test_read_edit_no_tools_masks_and_fail_closed_guard_are_composed(self):
        for kwargs, allowed in (({}, READ_TOOLS), ({'edit': True}, EDIT_TOOLS), ({'no_tools': True}, ())):
            invocation = self.build(**kwargs)
            self.assertEqual(invocation['allowed_tools'], list(allowed))
            overlay = json.loads(Path(invocation['config_path']).read_text(encoding='utf8'))
            indexed = {row['id']: row for row in overlay if 'id' in row}
            for key in ('tool-bash', 'tool-pwsh', 'tool-web', 'tool-subagent', 'session-title-llm', 'session-telemetry-otel'):
                self.assertTrue(indexed[key]['disabled'])
            self.assertIn('ossDshGuard', indexed['headless-runner']['inject'])
            self.assertEqual(indexed['agent-default-model']['config'], {'provider': 'tokenking', 'model': 'devin/swe-2'})
            provider = indexed['llm-pi-ai']['config']['providers']['tokenking']
            self.assertEqual(provider['retryPolicy']['maxRetries'], 0)
            self.assertEqual(provider['apiKeyEnv'], 'OSS_DSH_API_KEY')
            self.assertEqual(len(provider['models']), 1)

    def test_existing_opencode_provider_is_read_only_fallback(self):
        source = self.root / 'opencode.json'
        source.write_text(json.dumps({'provider': {'tokenking': {'options': {
            'baseURL': 'https://fixture.invalid/v1', 'apiKey': 'legacy-fixture-key'}}}}), encoding='utf8')
        before = source.read_bytes()
        self.config = {'dsh': {'provider_config_path': str(source)}}
        invocation = self.build()
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(invocation['env']['OSS_DSH_API_KEY'], 'legacy-fixture-key')
        self.assertNotIn('legacy-fixture-key', Path(invocation['config_path']).read_text())

    def test_explicit_missing_env_key_does_not_silently_fallback(self):
        self.config['dsh']['api_key_env'] = 'THIS_ENV_DOES_NOT_EXIST_OSS_TEST'
        with self.assertRaises(DSHError):
            self.build()

    def test_context_limits_reject_booleans_floats_strings_and_impossible_sizes(self):
        for field, value in (('max_tokens', True), ('max_tokens', 12.5), ('max_tokens', '100'),
                             ('max_tokens', 0), ('context_window', False), ('context_window', 32768)):
            with self.subTest(field=field, value=value):
                self.config['dsh'][field] = value
                with self.assertRaises(DSHError):
                    self.build()
                self.config['dsh'].pop(field)


class DSHDiscoveryTests(unittest.TestCase):
    def test_desktop_bundle_is_invoked_directly_without_shell_shim(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'DeepSeek Harness.exe').touch()
            (root / 'resources').mkdir()
            (root / 'resources/app.asar').touch()
            argv, env = discover_cli({'dsh': {'desktop_install_dir': folder}})
            self.assertEqual(argv[0], str(root / 'DeepSeek Harness.exe'))
            self.assertIn('--expose-internals', argv)
            self.assertTrue(argv[-1].endswith('cli.js'))
            self.assertEqual(env['ELECTRON_RUN_AS_NODE'], '1')

    def test_shell_shims_are_not_executed(self):
        with patch('dsh_adapter.shutil.which', return_value='C:/dsh.cmd'):
            with self.assertRaises(DSHError):
                discover_cli({'dsh': {'desktop_install_dir': 'missing-fixture-directory'}})


if __name__ == '__main__':
    unittest.main()
