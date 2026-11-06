import copy
import json
from pathlib import Path
import tempfile
import unittest

from contribute import Pipeline
from model_selection import execution_channel, implementation_channel, research_model, selected_model
from telemetry import model_call, run


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.config = {'model': 'tokenking/devin/swe-2', 'implementation_channel': 'dsh',
                       'research': {'model': 'tokenking/deepseek-v4.1-flash', 'model_channel': 'hermes'}}

    def test_research_family_uses_dedicated_model_without_changing_implementation_or_review(self):
        original = copy.deepcopy(self.config)
        for purpose in ('research', 'research-correction', 'survey', 'survey-correction',
                        'survey-0415', 'survey-0415-correction'):
            self.assertEqual(selected_model(self.config, purpose), 'tokenking/deepseek-v4.1-flash')
            self.assertEqual(selected_model(self.config, purpose, review=True), 'tokenking/devin/swe-2')
        for purpose in ('plan', 'repair', 'feedback', 'pr-body', 'auto-review', 'research-review', 'analyze'):
            self.assertEqual(selected_model(self.config, purpose), 'tokenking/devin/swe-2')
        self.config['review_model'] = 'review/independent'
        self.assertEqual(selected_model(self.config, 'research', review=True), 'review/independent')
        del self.config['review_model']
        self.assertEqual(self.config, original)

    def test_legacy_fallbacks_and_explicit_research_executor(self):
        self.assertEqual(implementation_channel({}), 'opencode')
        self.assertEqual(research_model({'model': 'legacy/model'}), 'legacy/model')
        self.assertEqual(selected_model({}, 'research'), 'tokenking/devin/swe-2')
        self.assertEqual(implementation_channel(self.config), 'dsh')
        self.assertEqual(execution_channel(self.config, 'repair'), 'dsh')
        self.config['research'].pop('model_channel')
        self.assertEqual(execution_channel(self.config, 'research'), 'opencode')
        for channel in ('opencode', 'dsh'):
            self.config['research']['model_channel'] = channel
            for purpose in ('research', 'research-correction', 'survey', 'survey-correction',
                            'survey-0415-correction'):
                self.assertEqual(execution_channel(self.config, purpose), channel)
        self.assertEqual(execution_channel(self.config, 'plan'), 'dsh')

    def test_invalid_channels_fail_without_silent_fallback(self):
        for channel in ('unknown', '', None, [], {}):
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                implementation_channel({'implementation_channel': channel})
            with self.subTest(research_channel=channel), self.assertRaises(ValueError):
                execution_channel({'research': {'model_channel': channel}}, 'research')


class RecordedCalls:
    def __init__(self, pipeline):
        self.p, self.db = pipeline, pipeline.db

    @model_call('implementation')
    def execute(self, task_id, purpose, fail=False):
        if fail:
            raise RuntimeError('connection reset by peer')

    @model_call('hermes', review=False)
    def research(self, task_id, purpose):
        pass

    @model_call('hermes')
    def review(self, task_id, purpose):
        pass


class ModelSelectionTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config = {'repositories': ['owner/repo'], 'model': 'implementation/model',
                  'review_model': 'review/model', 'implementation_channel': 'dsh',
                  'research': {'model': 'research/model', 'model_channel': 'opencode'}}
        (self.root / 'config.json').write_text(json.dumps(config))
        self.pipeline = Pipeline(self.root)
        self.addCleanup(self.pipeline.db.close)
        self.calls = RecordedCalls(self.pipeline)

    def test_runtime_selection_records_actual_channels_models_and_preserves_history(self):
        self.calls.execute('task', 'plan')
        first = dict(self.pipeline.db.execute('SELECT * FROM model_calls').fetchone())
        self.calls.execute('task', 'research-correction')
        self.calls.research('task', 'survey')
        self.calls.research('task', 'controller-owned-research-name')
        self.calls.review('task', 'research')
        self.pipeline.config['implementation_channel'] = 'opencode'
        self.calls.execute('task', 'repair')
        rows = list(self.pipeline.db.execute('SELECT * FROM model_calls ORDER BY started'))
        self.assertEqual([(r['channel'], r['model']) for r in rows], [
            ('dsh', 'implementation/model'), ('opencode', 'research/model'),
            ('hermes', 'research/model'), ('hermes', 'research/model'), ('hermes', 'review/model'),
            ('opencode', 'implementation/model')])
        self.assertEqual(dict(rows[0]), first)
        self.assertTrue(all(r['outcome'] == 'success' for r in rows))

    def test_failed_dynamic_call_is_budgeted_and_charged_to_actual_channel(self):
        with run(self.pipeline, 'test') as current:
            with self.assertRaisesRegex(RuntimeError, 'connection reset'):
                self.calls.execute('task', 'repair', fail=True)
            self.assertEqual(current.channel_failures, {'dsh': 1})
            self.assertEqual(current.counters['model_calls'], 1)
        row = self.pipeline.db.execute('SELECT * FROM model_calls').fetchone()
        self.assertEqual(row['channel'], 'dsh')
        self.assertEqual(row['model'], 'implementation/model')
        self.assertEqual(row['outcome'], 'network')


if __name__ == '__main__':
    unittest.main()
