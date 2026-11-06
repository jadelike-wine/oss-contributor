"""Capacity exhaustion must not spend network work on an ineligible backlog."""
import unittest
from unittest.mock import patch

from contribute import ContributionPaused
from scheduling import RunTaskBudget
import test_conversion_scheduling as fixtures


class DispatchCapacityTests(unittest.TestCase):
    setUp = fixtures.ConversionRegressionTests.setUp
    tearDown = fixtures.ConversionRegressionTests.tearDown
    issue_task = fixtures.ConversionRegressionTests.issue_task

    def test_capacity_probe_does_not_charge_task(self):
        budget = RunTaskBudget({'automation': {'max_tasks_per_tick': 1}})
        self.assertTrue(budget.can_admit('first', 'implementation'))
        self.assertEqual(set(), budget.used['implementation'])
        self.assertTrue(budget.admit('first', 'implementation'))
        self.assertTrue(budget.can_admit('first', 'implementation'))
        self.assertFalse(budget.can_admit('second', 'implementation'))
        self.assertTrue(budget.can_admit('second', 'maintenance'))

    def test_full_implementation_lane_skips_backlog_policy_reads_but_not_maintenance(self):
        keys = [self.issue_task('Owner/Repo', n) for n in range(1, 101)]
        maintenance = self.issue_task('Other/Repo', 101, 'PR_OPEN')
        budget = RunTaskBudget({'automation': {'max_tasks_per_tick': 1}})
        budget.admit('already-started', 'implementation')
        attempted = set()
        with patch.object(self.p, 'require_contribution') as policy, \
                patch.object(self.engine, 'advance') as advance:
            self.engine.dispatch_candidates([self.p.task(k) for k in keys + [maintenance]], budget, attempted)
        self.assertEqual([maintenance], [c.args[0]['id'] for c in policy.call_args_list])
        advance.assert_called_once_with(maintenance)
        self.assertEqual({maintenance}, attempted)

    def test_already_admitted_task_can_continue_when_lane_is_full(self):
        key = self.issue_task('Owner/Repo', 1, 'PATCH_READY')
        budget = RunTaskBudget({'automation': {'max_tasks_per_tick': 1}})
        budget.admit(key, 'implementation')
        with patch.object(self.p, 'require_contribution') as policy, \
                patch.object(self.engine, 'advance') as advance:
            self.engine.dispatch_candidates([self.p.task(key)], budget, set())
        policy.assert_called_once()
        advance.assert_called_once_with(key)

    def test_policy_denial_leaves_capacity_for_next_candidate(self):
        denied = self.issue_task('Owner/Repo', 1)
        ready = self.issue_task('Other/Repo', 2)
        budget = RunTaskBudget({'automation': {'max_tasks_per_tick': 1}})
        def check(task):
            if task['id'] == denied:
                raise ContributionPaused('Upstream requires ready label')
        with patch.object(self.p, 'require_contribution', side_effect=check), \
                patch.object(self.engine, 'advance') as advance:
            self.engine.dispatch_candidates([self.p.task(denied), self.p.task(ready)], budget, set())
        advance.assert_called_once_with(ready)
        self.assertEqual({ready}, budget.used['implementation'])


if __name__ == '__main__':
    unittest.main()
