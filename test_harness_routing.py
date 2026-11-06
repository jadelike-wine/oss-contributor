"""Exercise executor selection at the controller and service-circuit boundaries."""
import json
import copy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from autonomous import Autonomous
from contribute import Pipeline
from telemetry import BudgetExhausted, run
import test_research as research_fixtures


IMPLEMENTATION_MODEL = 'tokenking/devin/swe-2'
RESEARCH_MODEL = 'tokenking/deepseek-v4.1-flash'


def hermes_result(answer):
    return subprocess.CompletedProcess([], 0, json.dumps({
        'type': 'result', 'exit_code': 0, 'text': json.dumps(answer),
        'tokens': {'input': 10, 'output': 5}}), '')


class HarnessRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = {'repositories': ['owner/repo'], 'timeout_seconds': 30,
                  'model': IMPLEMENTATION_MODEL, 'review_model': IMPLEMENTATION_MODEL,
                  'implementation_channel': 'dsh', 'opencode_executable': 'opencode',
                  'research': {'model': RESEARCH_MODEL, 'model_channel': 'hermes'},
                  'automation': {'enabled': True, 'max_tasks_per_tick': 3}}
        (self.root / 'config.json').write_text(json.dumps(config), encoding='utf-8')
        self.p = Pipeline(self.root)
        self.addCleanup(self.p.db.close)
        self.engine = Autonomous(self.p)
        self.key = self.p.hunt('owner/repo', 'bounded routing fixture')
        self.work = self.root / 'workspace'
        self.work.mkdir()
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET source=?,number=?,workspace=?,base_sha=? WHERE id=?',
                              ('issue', 1, str(self.work), 'a' * 40, self.key))

    def calls(self):
        return [dict(row) for row in self.p.db.execute('SELECT * FROM model_calls ORDER BY started,call_id')]

    def test_dsh_implementation_receives_edit_permission_and_records_actual_channel(self):
        answer = {'summary': 'bounded patch'}
        with patch.object(self.engine, 'dsh_model', return_value=answer) as dsh, \
                patch('autonomous.opencode_executable') as opencode, patch('autonomous.execute') as execute:
            self.assertEqual(answer, self.engine.model(self.key, 'repair', 'Repair the fixture', edit=True))
        dsh.assert_called_once_with(self.key, 'repair', self.root / 'runs' / self.key / 'repair-input.txt', edit=True)
        self.assertEqual('Repair the fixture', dsh.call_args.args[2].read_text(encoding='utf-8'))
        opencode.assert_not_called()
        execute.assert_not_called()
        self.assertEqual([(row['channel'], row['model'], row['outcome']) for row in self.calls()],
                         [('dsh', IMPLEMENTATION_MODEL, 'success')])

    def test_legacy_config_still_uses_opencode_with_original_model(self):
        self.p.config.pop('implementation_channel')
        self.p.config.pop('research')
        answer = {'decision': 'wait', 'reason': 'No reproduction yet'}
        events = [
            {'type': 'text', 'part': {'messageID': 'm', 'text': json.dumps(answer)}},
            {'type': 'step_finish', 'part': {'messageID': 'm', 'id': 's', 'reason': 'stop',
                                           'tokens': {'input': 10, 'output': 5}}}]
        result = subprocess.CompletedProcess([], 0, '\n'.join(json.dumps(event) for event in events), '')
        with patch('autonomous.opencode_executable', return_value='fixture-opencode'), \
                patch('autonomous.execute', return_value=result) as invoke:
            self.assertEqual(answer, self.engine.model(self.key, 'plan', 'Plan the fixture', edit=True))
        argv = invoke.call_args.args[0]
        self.assertEqual('fixture-opencode', argv[0])
        self.assertEqual(IMPLEMENTATION_MODEL, argv[argv.index('--model') + 1])
        self.assertIn('--pure', argv)
        permissions = json.loads(invoke.call_args.kwargs['env']['OPENCODE_CONFIG_CONTENT'])['permission']
        self.assertEqual('allow', permissions['edit'])
        self.assertEqual('deny', permissions['*'])
        self.assertEqual('opencode', self.calls()[0]['channel'])
        self.assertEqual(IMPLEMENTATION_MODEL, self.calls()[0]['model'])

    def test_hermes_research_uses_flash_but_release_review_keeps_swe(self):
        outputs = [hermes_result({'decision': 'none', 'reason': 'No grounded candidate'}),
                   hermes_result({'approved': False, 'reason': 'Missing executable verification'})]
        with patch('autonomous.execute', side_effect=outputs) as invoke:
            self.engine.hermes_research(self.key, 'research', 'Inspect supplied evidence')
            review = self.engine.judge(self.key, 'auto-review', {'gate': 'Require executed verification'})
        self.assertEqual(2, invoke.call_count)
        for call, expected in zip(invoke.call_args_list, ('deepseek-v4.1-flash', 'devin/swe-2')):
            argv = call.args[0]
            self.assertEqual('tokenking', argv[argv.index('--provider') + 1])
            self.assertEqual(expected, argv[argv.index('--model') + 1])
            self.assertEqual('none', argv[argv.index('-t') + 1])
        self.assertEqual('same_model_separate_channel', review['review_identity']['independence'])
        self.assertEqual([(row['purpose'], row['channel'], row['model']) for row in self.calls()],
                         [('research', 'hermes', RESEARCH_MODEL), ('auto-review', 'hermes', IMPLEMENTATION_MODEL)])

    def test_research_review_identity_uses_recorded_generation_not_current_config(self):
        identities = [
            ({'model': RESEARCH_MODEL, 'channel': 'hermes'}, 'different_model_same_channel'),
            ({'model': IMPLEMENTATION_MODEL, 'channel': 'hermes'}, 'same_model_separate_invocation'),
            ({'model': IMPLEMENTATION_MODEL, 'channel': 'opencode'}, 'same_model_separate_channel'),
            ({'model': None, 'channel': None}, 'unknown'),
        ]
        for identity, expected in identities:
            for purpose in ('research-review', 'scope-overlap-review', 'duplicates-issue'):
                with self.subTest(identity=identity, purpose=purpose):
                    self.engine.store(self.key, {'stage': 'PLAN', 'attempts': {},
                                                'research_pending': {'model_identity': identity}})
                    with patch('autonomous.execute', return_value=hermes_result({'approved': True, 'reason': 'Fixture'})):
                        result = self.engine.judge(self.key, purpose, {'plan': {'category': 'bug'}})
                    self.assertEqual(expected, result['review_identity']['independence'])
                    self.assertEqual(IMPLEMENTATION_MODEL, result['review_identity']['model'])

    def test_failed_dsh_calls_stop_dsh_dispatch_without_opening_opencode_circuit(self):
        self.engine.store(self.key, {'stage': 'REPAIR', 'attempts': {}})
        with run(self.p, 'routing') as current, \
                patch.object(self.engine, 'dsh_model', side_effect=RuntimeError('DSH did not complete repair: missing terminal event')), \
                patch.object(self.p, 'require_contribution') as policy, patch.object(self.engine, 'advance') as advance:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'DSH did not complete repair'):
                    self.engine.model(self.key, 'repair', 'Repair the fixture', edit=True)
            self.assertEqual({'dsh': 2}, current.channel_failures)
            self.engine.dispatch_candidates([self.p.task(self.key)], current.task_budget, set())
            policy.assert_not_called()
            advance.assert_not_called()
            self.p.config['implementation_channel'] = 'opencode'
            self.engine.dispatch_candidates([self.p.task(self.key)], current.task_budget, set())
            advance.assert_called_once_with(self.key)
        self.assertEqual(['dsh', 'dsh'], [row['channel'] for row in self.calls()])
        self.assertEqual(['service_unavailable', 'service_unavailable'], [row['outcome'] for row in self.calls()])

    def test_old_opencode_circuit_does_not_block_dsh_advance(self):
        self.engine.store(self.key, {'stage': 'REPAIR', 'attempts': {}})
        def complete(key, data):
            self.engine.move(key, data, 'SKIPPED')
        with run(self.p, 'routing') as current, patch.object(self.p, 'require_contribution'), \
                patch.object(self.engine, 'repair', side_effect=complete) as repair:
            current.channel_failures['opencode'] = 2
            self.assertEqual('SKIPPED', self.engine.advance(self.key))
            repair.assert_called_once()

    def test_dsh_circuit_blocks_direct_advance_but_not_hermes_review(self):
        self.engine.store(self.key, {'stage': 'REPAIR', 'attempts': {}})
        def complete(key, data):
            self.engine.move(key, data, 'SKIPPED')
        with run(self.p, 'routing') as current, patch.object(self.p, 'require_contribution'), \
                patch.object(self.engine, 'repair') as repair, \
                patch.object(self.engine, 'review', side_effect=complete) as review:
            current.channel_failures['dsh'] = 2
            self.assertEqual('REPAIR', self.engine.advance(self.key))
            repair.assert_not_called()
            self.engine.store(self.key, {'stage': 'AUTO_REVIEW', 'attempts': {}})
            self.assertEqual('SKIPPED', self.engine.advance(self.key))
            review.assert_called_once()


class ResearchProvenanceTests(unittest.TestCase):
    setUp = research_fixtures.ResearchTests.setUp
    tearDown = research_fixtures.ResearchTests.tearDown
    git = research_fixtures.ResearchTests.git
    model = research_fixtures.ResearchTests.model
    mocks = research_fixtures.ResearchTests.mocks
    fake_gh = research_fixtures.ResearchTests.fake_gh
    fake_command = research_fixtures.ResearchTests.fake_command
    packet = research_fixtures.ResearchTests.packet
    research_mocks = research_fixtures.ResearchTests.research_mocks

    def pending_research(self):
        packet = self.packet()
        self.p.config.update(model=IMPLEMENTATION_MODEL, research={
            'model': IMPLEMENTATION_MODEL, 'model_channel': 'hermes'})
        with self.research_mocks(research_fixtures.opportunity(), packet), \
                patch.object(self.engine, 'hermes_research', return_value=research_fixtures.opportunity()), \
                patch.object(self.engine, 'duplicates', side_effect=BudgetExhausted('run_model_calls_exhausted')):
            with self.assertRaises(BudgetExhausted):
                self.engine.research(self.key, self.engine.load(self.key))
        pending = self.engine.load(self.key)['research_pending']
        self.assertEqual({'model': IMPLEMENTATION_MODEL, 'channel': 'hermes'}, pending['model_identity'])
        self.p.config['research']['model'] = RESEARCH_MODEL
        return packet

    def test_reused_checkpoint_preserves_its_original_model_identity(self):
        packet = self.pending_research()
        original = copy.deepcopy(self.engine.load(self.key)['research_pending']['model_identity'])
        with self.research_mocks(research_fixtures.opportunity(), packet), \
                patch.object(self.engine, 'hermes_research') as generate, \
                patch.object(self.engine, 'duplicates', return_value=True):
            result = self.engine.research(self.key, self.engine.load(self.key))
        generate.assert_not_called()
        self.assertEqual(original, result['model_identity'])
        self.assertEqual(original, self.engine.load(self.key)['research']['model_identity'])

    def test_legacy_checkpoint_identity_stays_unknown_after_model_switch(self):
        packet = self.pending_research()
        data = self.engine.load(self.key)
        del data['research_pending']['model_identity']
        self.engine.store(self.key, data)
        with self.research_mocks(research_fixtures.opportunity(), packet), \
                patch.object(self.engine, 'hermes_research') as generate, \
                patch.object(self.engine, 'duplicates', return_value=True):
            result = self.engine.research(self.key, self.engine.load(self.key))
        generate.assert_not_called()
        self.assertEqual({'model': None, 'channel': None}, result['model_identity'])
        self.assertEqual(result['model_identity'], self.engine.load(self.key)['research']['model_identity'])

    def test_correction_records_new_generator_and_ignores_model_claimed_identity(self):
        packet = self.pending_research()
        data = self.engine.load(self.key)
        data['research_pending']['opportunity']['scope'] = []
        self.engine.store(self.key, data)
        corrected = research_fixtures.opportunity()
        corrected['model_identity'] = {'model': 'untrusted/claim', 'channel': 'opencode'}
        with self.research_mocks(corrected, packet), \
                patch.object(self.engine, 'hermes_research', return_value=corrected) as generate, \
                patch.object(self.engine, 'duplicates', return_value=True):
            result = self.engine.research(self.key, self.engine.load(self.key))
        generate.assert_called_once()
        self.assertEqual('research-correction', generate.call_args.args[1])
        self.assertEqual({'model': RESEARCH_MODEL, 'channel': 'hermes'}, result['model_identity'])
        self.assertEqual(result['model_identity'], self.engine.load(self.key)['research']['model_identity'])


if __name__ == '__main__':
    unittest.main()
