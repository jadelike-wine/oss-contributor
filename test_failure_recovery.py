import copy
import unittest
from unittest.mock import patch

from failures import recover_dependency_setup
import test_conversion_runtime as runtime_fixtures


def legacy_dependency():
    return {'stage': 'REPRODUCE', 'attempts': {},
            'last_error': 'Baseline setup failed; not a reproduction: npm error ERESOLVE could not resolve dependency',
            'infrastructure_failures': {'REPRODUCE': {
                'category': 'network', 'count': 3, 'exhausted': True,
                'retry_after': '2026-11-01T00:00:00+00:00'}},
            'stop_reason': 'infrastructure_retry_exhausted'}


class DependencyRecoveryTests(unittest.TestCase):
    def test_exact_legacy_collision_is_replanned_once_and_history_is_preserved(self):
        data = legacy_dependency()
        original = copy.deepcopy(data)
        self.assertTrue(recover_dependency_setup(data, {}))
        self.assertEqual('PLAN', data['stage'])
        self.assertEqual({'REPRODUCE': 1}, data['attempts'])
        self.assertEqual(original['last_error'], data['last_error'])
        self.assertEqual(original['infrastructure_failures']['REPRODUCE'],
                         data['failure_reclassifications'][0]['previous'])
        self.assertEqual({}, data['infrastructure_failures'])
        self.assertFalse(recover_dependency_setup(data, {}))
        self.assertEqual(1, len(data['failure_reclassifications']))

    def test_existing_recipe_limit_is_not_reset(self):
        data = legacy_dependency()
        data['attempts']['REPRODUCE'] = 2
        self.assertTrue(recover_dependency_setup(data, {'max_stage_attempts': 3}))
        self.assertEqual('FAILED', data['stage'])
        self.assertEqual(3, data['attempts']['REPRODUCE'])

    def test_other_exhausted_failures_and_stages_are_unchanged(self):
        for change in ('dns', 'service', 'stage', 'missing_provenance'):
            data = legacy_dependency()
            if change == 'dns':
                data['last_error'] = 'Baseline setup failed; not a reproduction: Could not resolve host: registry.npmjs.org'
            elif change == 'service':
                data['infrastructure_failures']['REPRODUCE']['category'] = 'service_unavailable'
            elif change == 'stage':
                data['stage'] = 'FAILED'
            else:
                data['last_error'] = 'npm error ERESOLVE could not resolve dependency'
            original = copy.deepcopy(data)
            with self.subTest(change=change):
                self.assertFalse(recover_dependency_setup(data, {}))
                self.assertEqual(original, data)


class DependencySchedulerRecoveryTests(unittest.TestCase):
    setUp = runtime_fixtures.RuntimeConversionTests.setUp
    tearDown = runtime_fixtures.RuntimeConversionTests.tearDown
    issue = runtime_fixtures.RuntimeConversionTests.issue

    def test_scheduler_replans_exhausted_setup_instead_of_retrying_install(self):
        key = self.issue(1, 'REPRODUCE')
        self.engine.store(key, legacy_dependency())
        with patch.object(self.engine, 'plan') as plan, patch.object(self.engine, 'reproduce') as reproduce:
            self.engine.run()
        plan.assert_called_once()
        reproduce.assert_not_called()
        data = self.engine.load(key)
        self.assertEqual('PLAN', self.p.task(key)['state'])
        self.assertEqual(1, len(data['failure_reclassifications']))


if __name__ == '__main__':
    unittest.main()
