import unittest
from unittest.mock import Mock

from ci_status import observe_ci


class CIObservationTests(unittest.TestCase):
    def pr(self):
        return {'headRefOid': 'exact-head', 'statusCheckRollup': [
            {'name': 'standards', 'conclusion': 'SUCCESS'}]}

    def workflow(self, **changes):
        return dict({'id': 1, 'workflow_id': 7, 'head_sha': 'exact-head',
                     'name': 'test', 'status': 'completed', 'conclusion': 'action_required',
                     'event': 'pull_request', 'html_url': 'https://github.com/o/r/actions/runs/1'}, **changes)

    def test_green_rollup_does_not_hide_workflow_approval(self):
        gh = Mock(return_value=[{'workflow_runs': [self.workflow()]}])
        ci = observe_ci('o/r', self.pr(), gh)
        self.assertEqual('approval_required', ci['state'])
        self.assertEqual([], ci['failed_checks'])
        self.assertEqual('approval_required', ci['waiting_checks'][0]['reason'])
        self.assertIn('--paginate', gh.call_args.args)
        self.assertIn('head_sha=exact-head', gh.call_args.args[-1])

    def test_paginated_runs_supersede_old_attempt_and_ignore_other_head(self):
        gh = Mock(return_value=[{'workflow_runs': [self.workflow(id=3, conclusion='success')]},
                                {'workflow_runs': [self.workflow(id=1, conclusion='failure'),
                                                   self.workflow(id=4, head_sha='old', conclusion='failure')]}])
        ci = observe_ci('o/r', self.pr(), gh)
        self.assertEqual('observed_checks_passed', ci['state'])
        self.assertEqual([3], [r['id'] for r in ci['workflow_runs']])

    def test_workflow_query_failure_cannot_report_all_green(self):
        for response in ({}, [{}], [{'workflow_runs': [{}]}]):
            with self.subTest(response=response):
                ci = observe_ci('o/r', self.pr(), Mock(return_value=response))
                self.assertEqual('unknown', ci['state'])
                self.assertIn('error', ci)
        ci = observe_ci('o/r', self.pr(), Mock(side_effect=RuntimeError('Access denied')))
        self.assertEqual('unknown', ci['state'])
        self.assertEqual([], ci['failed_checks'])

    def test_pending_cancelled_and_failed_are_distinct(self):
        for changes, expected in (({'status': 'queued', 'conclusion': None}, 'pending'),
                                  ({'conclusion': 'cancelled'}, 'unknown'),
                                  ({'conclusion': 'failure'}, 'failed')):
            with self.subTest(changes=changes):
                ci = observe_ci('o/r', self.pr(), Mock(return_value=[{'workflow_runs': [self.workflow(**changes)]}]))
                self.assertEqual(expected, ci['state'])

    def test_action_required_in_rollup_is_not_test_failure(self):
        pr = self.pr()
        pr['statusCheckRollup'].append({'name': 'test', 'conclusion': 'ACTION_REQUIRED'})
        ci = observe_ci('o/r', pr, Mock(return_value=[{'workflow_runs': []}]))
        self.assertEqual('approval_required', ci['state'])
        self.assertEqual([], ci['failed_checks'])


if __name__ == '__main__':
    unittest.main()
