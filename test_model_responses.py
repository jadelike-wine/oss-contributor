import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from autonomous import Autonomous
from contribute import Pipeline
from feature_matrix import build
from model_responses import recover_research_json
from research import validate
from test_feature_matrix import matrix_answer


def stream(text, *, exit_code=0):
    split = len(text) // 2
    return '\n'.join(json.dumps(event) for event in [
        {'type': 'system'}, {'type': 'text', 'text': text[:split]},
        {'type': 'text', 'text': text[split:]},
        {'type': 'result', 'exit_code': exit_code, 'text': text}])


class ResearchJsonRecoveryTests(unittest.TestCase):
    def test_one_missing_root_closer_preserves_nested_values_and_receipt(self):
        answer = {'decision': 'candidate', 'reason': 'Quoted "{[}]" and \\ remain literal.',
                  'feature_matrix': {'feature_status': {'owner/repo': {'evidence': [
                      {'quote': 'Unicode 路径 with braces { and }'}]}}}}
        raw = ' \n' + json.dumps(answer, ensure_ascii=False)[:-1] + '\n '
        recovered, receipt = recover_research_json(stream(raw), 'research', 0)
        self.assertEqual(recovered, answer)
        self.assertEqual(receipt['raw_text_sha256'], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(receipt['normalized_text_sha256'],
                         hashlib.sha256((raw.strip() + '}').encode()).hexdigest())
        self.assertEqual(receipt['schema_validation'], 'pending')
        self.assertEqual(receipt['evidence_validation'], 'pending')
        self.assertEqual(receipt['approval'], 'not_granted')

    def test_other_syntax_errors_and_ambiguous_values_are_never_normalized(self):
        cases = [
            '{"decision":"candidate","feature_matrix":{"feature_status":{}',
            '{"decision":"candidate","reason":"unfinished',
            '{"decision":"none",',
            '{"decision":"none","reason" "invalid"',
            '{"decision":"none"} {"decision":"candidate"',
            '{"decision":"none","decision":"candidate"',
            '{"decision":"candidate","nested":{"a":1,"a":2}',
            '{"decision":"none","value":NaN',
            '{"decision":"none","value":Infinity',
            '{"decision":"none","value":1e999',
            '```json\n{"decision":"none"\n```',
            '[{"decision":"none"}',
            '{"decision":"none"}',
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertIsNone(recover_research_json(stream(raw), 'research', 0))

    def test_recovery_requires_one_matching_successful_no_tool_terminal_stream(self):
        raw = '{"decision":"none","reason":"insufficient evidence"'
        output = stream(raw)
        events = [json.loads(line) for line in output.splitlines()]
        cases = {
            'nonzero_process': (output, 1),
            'nonzero_result': (stream(raw, exit_code=1), 0),
            'bool_exit_code': (stream(raw, exit_code=False), 0),
            'missing_result': ('\n'.join(output.splitlines()[:-1]), 0),
            'result_only': (output.splitlines()[-1], 0),
            'duplicate_result': (output + '\n' + output.splitlines()[-1], 0),
            'text_after_result': (output + '\n' + json.dumps(events[1]), 0),
            'malformed_line': ('warning: incomplete stream\n' + output, 0),
            'banner_after_events': (output + '\nWarning: Unknown toolsets: none', 0),
            'different_toolset_banner': ('Warning: Unknown toolsets: shell\n' + output, 0),
            'duplicate_banner': ('Warning: Unknown toolsets: none\nWarning: Unknown toolsets: none\n' + output, 0),
            'duplicate_stream_key': (output.replace('"exit_code": 0', '"exit_code": 1,"exit_code": 0'), 0),
            'mismatched_fragments': (output.replace('insufficient evidence', 'different', 1), 0),
            'nondict_event': ('[]\n' + output, 0),
        }
        for kind in ('tool_use', 'tool_call', 'tool_start', 'tool_result', 'error', 'unknown'):
            cases[kind] = (json.dumps({'type': kind}) + '\n' + output, 0)
        for name, (candidate, returncode) in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(recover_research_json(candidate, 'research', returncode))
        self.assertIsNotNone(recover_research_json(output, 'research-correction', 0))
        _, receipt = recover_research_json('Warning: Unknown toolsets: none\n' + output, 'research', 0)
        self.assertEqual(receipt['ignored_cli_banners'], ['Warning: Unknown toolsets: none'])
        for purpose in ('auto-review', 'research-review', 'recipe-review', 'plan', 'repair'):
            with self.subTest(purpose=purpose):
                self.assertIsNone(recover_research_json(output, purpose, 0))

    def test_normalized_research_still_fails_exact_source_evidence_gate(self):
        task = {'repo': 'owner/repo', 'base_sha': '0' * 40, 'workspace': '.'}
        answer = matrix_answer(task, [])
        answer['category'] = 'bug'
        packet = {'schema_version': 2, 'policy': {'CONTRIBUTING.md': 'Small bug fixes welcome.'},
                  'target': {'repo': task['repo'], 'sha': task['base_sha'],
                             'files': {'value.py': 'VALUE = 1', 'CONTRIBUTING.md': 'Small bug fixes welcome.'}},
                  'peers': []}
        validate(copy.deepcopy(answer), task, packet)
        build(answer, task, packet)
        answer['evidence'][0]['quote'] = 'invented source content'
        status = answer['feature_matrix']['feature_status']['owner/repo']
        status['evidence'] = [copy.deepcopy(answer['evidence'][0])]
        normalized, _ = recover_research_json(stream(json.dumps(answer)[:-1]), 'research', 0)
        with self.assertRaisesRegex(ValueError, 'quote does not match'):
            validate(copy.deepcopy(normalized), task, packet)
        with self.assertRaisesRegex(ValueError, 'quote does not match'):
            build(normalized, task, packet)


class HermesResearchRecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config.json').write_text(json.dumps({
            'repositories': ['owner/repo'], 'model': 'test/model', 'timeout_seconds': 30}))
        self.pipeline = Pipeline(self.root)
        self.addCleanup(self.pipeline.db.close)
        self.engine = Autonomous(self.pipeline)
        self.key = self.pipeline.hunt('owner/repo', 'bounded research')

    def test_research_retains_original_counts_one_call_and_grants_no_approval(self):
        raw = '{"decision":"none","reason":"no grounded candidate"'
        output = 'Warning: Unknown toolsets: none\n' + stream(raw)
        with patch('autonomous.execute', return_value=subprocess.CompletedProcess([], 0, output, '')) as execute:
            answer = self.engine.hermes_research(self.key, 'research', 'Find one grounded candidate')
        self.assertEqual(answer, {'decision': 'none', 'reason': 'no grounded candidate'})
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0][execute.call_args.args[0].index('-t') + 1], 'none')
        folder = self.engine.packet_dir(self.key)
        receipt = json.loads((folder / 'research-format-normalization.json').read_text())
        self.assertEqual(Path(receipt['raw_response_file']).read_text(), raw)
        self.assertEqual(hashlib.sha256(Path(receipt['raw_response_file']).read_bytes()).hexdigest(),
                         receipt['raw_text_sha256'])
        self.assertEqual(receipt['model'], 'test/model')
        self.assertEqual(receipt['approval'], 'not_granted')
        self.assertEqual((folder / 'research-review.jsonl').read_text(), output)
        self.assertEqual(self.pipeline.db.execute('SELECT count(*) FROM model_calls').fetchone()[0], 1)
        self.assertEqual(self.pipeline.db.execute('SELECT count(*) FROM research_candidates').fetchone()[0], 0)

    def test_judge_never_recovers_approval_even_with_research_purpose(self):
        output = stream('{"approved":true,"reason":"looks good"')
        with patch('autonomous.execute', return_value=subprocess.CompletedProcess([], 0, output, '')):
            for purpose in ('auto-review', 'research'):
                with self.subTest(purpose=purpose), self.assertRaises(json.JSONDecodeError):
                    self.engine.judge(self.key, purpose, {})
        self.assertFalse(list(self.engine.packet_dir(self.key).glob('*-format-normalization.json')))


if __name__ == '__main__':
    unittest.main()
