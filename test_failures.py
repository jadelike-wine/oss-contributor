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


if __name__ == '__main__':
    unittest.main()
