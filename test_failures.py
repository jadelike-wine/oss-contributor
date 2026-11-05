import datetime as dt
import json
import unittest

from failures import category, record_failure, retry_ready


class FailureTests(unittest.TestCase):
    def test_domains_are_distinct(self):
        examples = [('certificate verify failed', 'certificate'), ('HTTP 429 rate limit', 'rate_limit'),
                    ('connection reset by peer', 'network'), ('upstream stream closed before terminal event', 'service_unavailable'),
                    ('quote does not match evidence', 'insufficient_evidence'), ('regression assertion failed', 'patch_error')]
        for text, expected in examples:
            with self.subTest(expected=expected):
                self.assertEqual(expected, category(RuntimeError(text)))
        self.assertEqual('model_format', category(json.JSONDecodeError('invalid', '', 0)))

    def test_cooldown_is_bounded_without_quality_penalty(self):
        data = {'attempts': {'PLAN': 1}}
        at = dt.datetime(2026, 10, 31, tzinfo=dt.timezone.utc)
        for attempt in range(3):
            self.assertTrue(record_failure(data, 'PLAN', ConnectionError('offline'), {}, at))
            self.assertFalse(retry_ready(data, 'PLAN', at))
            self.assertEqual({'PLAN': 1}, data['attempts'])
        self.assertFalse(retry_ready(data, 'PLAN', at + dt.timedelta(days=1)))
        self.assertEqual('infrastructure_retry_exhausted', data['stop_reason'])

    def test_patch_failure_still_uses_quality_path(self):
        self.assertFalse(record_failure({}, 'PATCH_READY', RuntimeError('assertion failed'), {}))

    def test_dependency_resolution_conflict_is_not_retried_as_network(self):
        # Production PrimeReact setup output: retrying identical npm install
        # cannot reconcile jspdf-autotable's peer range with jspdf 4.
        output = ('Baseline setup failed; not a reproduction:\n'
                  'npm error code ERESOLVE\nnpm error ERESOLVE could not resolve\n'
                  'npm error Found: jspdf@4.0.0\n'
                  'npm error Could not resolve dependency:\n'
                  'npm error peer jspdf@"^2 || ^3" from jspdf-autotable@5.0.2\n')
        data = {'attempts': {}}
        self.assertFalse(record_failure(data, 'REPRODUCE', RuntimeError(output), {}))
        self.assertEqual('dependency_conflict', data['failure_category'])
        self.assertNotIn('infrastructure_failures', data)
        for message in ('unable to resolve dependency tree', 'ResolutionImpossible'):
            with self.subTest(message=message):
                self.assertEqual('dependency_conflict', category(RuntimeError(message)))

    def test_dns_resolution_failures_remain_transient_network_errors(self):
        for message in ('Could not resolve host: github.com', 'Could not resolve hostname github.com',
                        'Could not resolve proxy: proxy.example', 'getaddrinfo ENOTFOUND registry.npmjs.org',
                        'Temporary failure in name resolution', 'Name or service not known', 'EAI_AGAIN'):
            with self.subTest(message=message):
                data = {'attempts': {}}
                self.assertTrue(record_failure(data, 'REPRODUCE', RuntimeError(message), {}))
                self.assertEqual('network', data['failure_category'])
                self.assertEqual({}, data['attempts'])

    def test_structural_model_output_does_not_consume_candidate_quality_attempts(self):
        from autonomous import validate_spec
        data = {'attempts': {'PLAN': 1}}
        with self.assertRaises(ValueError) as caught:
            validate_spec({'setup': []})
        self.assertTrue(record_failure(data, 'PLAN', caught.exception, {}))
        self.assertEqual('model_format', data['failure_category'])
        self.assertEqual({'PLAN': 1}, data['attempts'])
        self.assertEqual('model_format', category(ValueError('Missing opportunity field: users')))


if __name__ == '__main__':
    unittest.main()
