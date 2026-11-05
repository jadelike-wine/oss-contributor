import json
import subprocess
import unittest
from unittest.mock import patch

import test_telemetry as fixtures
from autonomous import Autonomous, model_timeout


class ResearchTimeoutTests(unittest.TestCase):
    setUp = fixtures.TelemetryTests.setUp
    tearDown = fixtures.TelemetryTests.tearDown

    def test_timeout_applies_only_to_research_generation_and_correction(self):
        config = {'timeout_seconds': 1500, 'timeouts': {'research_model_seconds': 240}}
        for purpose in ('research', 'research-correction'):
            self.assertEqual(240, model_timeout(config, purpose))
        for purpose in ('plan', 'repair', 'research-review', 'duplicates-issue'):
            self.assertEqual(1500, model_timeout(config, purpose))
        self.assertEqual(99, model_timeout({'timeout_seconds': 99}, 'research'))

    def test_hermes_research_adapter_passes_specific_timeout(self):
        self.p.config.update(model='provider/model', timeouts={'model_seconds': 1500, 'research_model_seconds': 240})
        task = self.p.hunt('owner/repo', 'a bounded source question')
        result = {'type': 'result', 'exit_code': 0, 'text': '{"decision":"none","reason":"no defect"}'}
        engine = Autonomous(self.p)
        with patch('autonomous.execute', return_value=subprocess.CompletedProcess([], 0, json.dumps(result), '')) as child:
            engine.hermes_research(task, 'research', 'inspect supplied source')
        self.assertEqual(240, child.call_args.kwargs['timeout'])


if __name__ == '__main__':
    unittest.main()
