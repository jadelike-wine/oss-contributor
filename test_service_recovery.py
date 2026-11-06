import copy
import datetime as dt
import json
import sqlite3
import unittest

import failures


NOW = dt.datetime(2026, 11, 5, 5, 40, tzinfo=dt.timezone.utc)
TASK = 'Tencent--tdesign-react--hunt-example'


class ModelServiceRecoveryTests(unittest.TestCase):
    channel = 'opencode'
    client = 'OpenCode'

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE model_calls (call_id TEXT PRIMARY KEY, run_id TEXT, '
                        'task TEXT, purpose TEXT, model TEXT, channel TEXT, started TEXT, '
                        'ended TEXT, outcome TEXT, input_tokens INTEGER, output_tokens INTEGER, '
                        'actual_cost_usd REAL)')
        self.call('failure', TASK, '2026-11-01T10:56:00+00:00',
                  '2026-11-01T10:57:47.625486+00:00', 'service_unavailable')
        self.call('success', 'other-task', '2026-11-05T03:23:32+00:00',
                  '2026-11-05T03:26:40+00:00', 'success')
        self.data = {
            'stage': 'PLAN', 'attempts': {'PLAN': 1, 'PATCH_READY': 2},
            'last_error': f'{self.client} did not complete plan: Service temporarily unavailable',
            'failure_category': 'service_unavailable',
            'stop_reason': 'infrastructure_retry_exhausted',
            'infrastructure_failures': {
                'PLAN': {'category': 'service_unavailable', 'count': 3,
                         'at': '2026-11-01T10:57:47.631544+00:00',
                         'retry_after': '2026-11-01T11:17:47.631544+00:00', 'exhausted': True},
                'RESEARCH': {'category': 'network', 'count': 1}},
        }

    def call(self, call_id, task, started, ended, outcome, purpose='plan',
             model='tokenking/devin/swe-2', channel=None):
        self.db.execute('INSERT INTO model_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        (call_id, 'run-' + call_id, task, purpose, model, channel or self.channel,
                         started, ended, outcome, 100, 20, 0.01))

    def recover(self, data=None, options=None):
        return failures.recover_model_service(self.db, self.data if data is None else data,
                                              TASK, options or {}, NOW)

    def assert_unchanged(self):
        original = copy.deepcopy(self.data)
        self.assertFalse(self.recover())
        self.assertEqual(original, self.data)

    def test_recovers_once_with_complete_receipts_and_no_budget_or_db_reset(self):
        original = copy.deepcopy(self.data)
        calls = list(self.db.execute('SELECT * FROM model_calls ORDER BY call_id'))
        changes = self.db.total_changes
        self.assertTrue(self.recover())
        self.assertEqual('PLAN', self.data['stage'])
        self.assertEqual(original['attempts'], self.data['attempts'])
        self.assertEqual(original['last_error'], self.data['last_error'])
        self.assertEqual({'RESEARCH': original['infrastructure_failures']['RESEARCH']},
                         self.data['infrastructure_failures'])
        self.assertTrue(failures.retry_ready(self.data, 'PLAN', NOW))
        receipt = self.data['model_service_recoveries'][0]
        self.assertEqual(TASK, receipt['task'])
        self.assertEqual('PLAN', receipt['stage'])
        self.assertEqual(original['infrastructure_failures']['PLAN'], receipt['previous'])
        self.assertEqual(original['last_error'], receipt['error'])
        columns = [x[1] for x in self.db.execute('PRAGMA table_info(model_calls)')]
        self.assertEqual(dict(zip(columns, calls[0])), receipt['failed_call'])
        self.assertEqual(dict(zip(columns, calls[1])), receipt['success_call'])
        self.assertEqual(changes, self.db.total_changes)
        self.assertEqual(calls, list(self.db.execute('SELECT * FROM model_calls ORDER BY call_id')))
        self.assertEqual('model_service_recovered_once', self.data['stop_reason'])
        # The next outage still has the configured three-attempt limit; it
        # cannot obtain a second reset from the same or another success.
        for _ in range(3):
            failures.record_failure(self.data, 'PLAN', RuntimeError(original['last_error']), {}, NOW)
        self.assertFalse(failures.retry_ready(self.data, 'PLAN', NOW))
        self.assertFalse(self.recover())
        self.assertEqual(1, len(self.data['model_service_recoveries']))
        self.assertEqual(original['attempts'], self.data['attempts'])
        json.dumps(self.data)

    def test_repair_requires_repair_success_and_is_independent_of_plan_recovery(self):
        self.data['stage'] = 'REPAIR'
        self.data['last_error'] = self.data['last_error'].replace('plan:', 'repair:')
        self.data['infrastructure_failures']['REPAIR'] = self.data['infrastructure_failures'].pop('PLAN')
        self.data['model_service_recoveries'] = [{'task': TASK, 'stage': 'PLAN'}]
        self.db.execute("UPDATE model_calls SET purpose='repair'")
        self.db.row_factory = sqlite3.Row
        self.assertTrue(self.recover())
        self.assertEqual(['PLAN', 'REPAIR'], [r['stage'] for r in self.data['model_service_recoveries']])

    def test_nonmatching_failure_domains_stages_and_provenance_are_unchanged(self):
        original = copy.deepcopy(self.data)
        changes = [
            ('stage', 'REPRODUCE'), ('stage', 'FAILED'), ('stage', 'AUTO_REVIEW'),
            ('last_error', 'git failed: Service temporarily unavailable'),
            ('last_error', 'Hermes did not complete plan: Service temporarily unavailable'),
            ('last_error', f'{self.client} did not complete repair: Service temporarily unavailable'),
            ('last_error', f'{self.client} did not complete plan: unknown certificate verification error'),
            ('last_error', f'{self.client} did not complete plan: invalid model response'),
            ('failure_category', 'patch_error'),
        ]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                self.data = copy.deepcopy(original)
                self.data[key] = value
                self.assert_unchanged()
        for kind in ('network', 'certificate', 'model_format', 'patch_error'):
            with self.subTest(category=kind):
                self.data = copy.deepcopy(original)
                self.data['infrastructure_failures']['PLAN']['category'] = kind
                self.assert_unchanged()
        self.data = copy.deepcopy(original)
        self.data['infrastructure_failures']['PLAN']['exhausted'] = False
        self.assert_unchanged()

    def test_missing_wrong_channel_model_or_purpose_success_cannot_recover(self):
        other_channel = 'dsh' if self.channel == 'opencode' else 'opencode'
        for column, value in [('channel', 'hermes'), ('channel', other_channel), ('model', 'another-model'),
                              ('purpose', 'research-review'), ('outcome', 'failed'), ('ended', None)]:
            with self.subTest(column=column):
                self.db.execute(f'UPDATE model_calls SET {column}=? WHERE call_id=?', (value, 'success'))
                self.assert_unchanged()
                self.db.execute("DELETE FROM model_calls WHERE call_id='success'")
                self.call('success', 'other-task', '2026-11-05T03:23:32+00:00',
                          '2026-11-05T03:26:40+00:00', 'success')
        self.db.execute("DELETE FROM model_calls WHERE call_id='success'")
        self.assert_unchanged()

    def test_success_must_be_recent_finished_and_started_after_failure(self):
        for started, ended in [
            ('2026-11-03T01:00:00+00:00', '2026-11-03T01:01:00+00:00'),
            ('2026-11-05T06:00:00+00:00', '2026-11-05T06:01:00+00:00'),
            ('2026-11-01T10:56:00+00:00', '2026-11-05T03:26:40+00:00'),
            ('2026-11-05T04:00:00+00:00', '2026-11-05T03:00:00+00:00'),
            ('invalid', '2026-11-05T03:00:00+00:00'),
            ('2026-11-05T02:00:00', '2026-11-05T03:00:00'),
        ]:
            with self.subTest(started=started, ended=ended):
                self.db.execute("UPDATE model_calls SET started=?,ended=? WHERE call_id='success'", (started, ended))
                self.assert_unchanged()

    def test_failed_call_must_match_record_timestamp_and_latest_task_purpose(self):
        original = copy.deepcopy(self.data)
        for stamp in ('2026-11-01T10:58:00+00:00', '2026-11-01T10:57:40+00:00',
                      '2026-11-01T10:57:47', 'invalid', None):
            with self.subTest(stamp=stamp):
                self.data = copy.deepcopy(original)
                self.data['infrastructure_failures']['PLAN']['at'] = stamp
                self.assert_unchanged()
        self.data = original
        self.call('later-task-call', TASK, '2026-11-02T10:00:00+00:00', None, 'running')
        self.assert_unchanged()

    def test_failed_call_wrong_outcome_channel_or_missing_completion_is_rejected(self):
        other_channel = 'dsh' if self.channel == 'opencode' else 'opencode'
        for column, value in [('outcome', 'model_format'), ('channel', 'hermes'),
                              ('channel', other_channel), ('ended', None)]:
            with self.subTest(column=column):
                self.db.execute(f'UPDATE model_calls SET {column}=? WHERE call_id=?', (value, 'failure'))
                self.assert_unchanged()
                self.db.execute("DELETE FROM model_calls WHERE call_id='failure'")
                self.call('failure', TASK, '2026-11-01T10:56:00+00:00',
                          '2026-11-01T10:57:47.625486+00:00', 'service_unavailable')
        self.db.execute("DELETE FROM model_calls WHERE call_id='failure'")
        self.assert_unchanged()

    def test_quality_attempt_limit_and_prior_stage_recovery_remain_closed(self):
        self.data['attempts']['PLAN'] = 3
        self.assert_unchanged()
        self.data['attempts']['PLAN'] = 1
        self.data['model_service_recoveries'] = [{'task': TASK, 'stage': 'PLAN'}]
        self.assert_unchanged()

    def test_executor_prefix_must_match_failed_receipt_channel(self):
        other_client = 'DSH' if self.client == 'OpenCode' else 'OpenCode'
        self.data['last_error'] = self.data['last_error'].replace(self.client, other_client)
        self.assert_unchanged()

    def test_incomplete_executor_calls_keep_bounded_infrastructure_retries(self):
        data = {'stage': 'PLAN', 'attempts': {'PLAN': 1}}
        error = RuntimeError(f'{self.client} did not complete plan: missing terminal event')
        for _ in range(3):
            self.assertTrue(failures.record_failure(data, 'PLAN', error, {}, NOW))
        self.assertEqual('service_unavailable', data['failure_category'])
        self.assertEqual('infrastructure_retry_exhausted', data['stop_reason'])
        self.assertEqual({'PLAN': 1}, data['attempts'])
        self.assertFalse(failures.retry_ready(data, 'PLAN', NOW + dt.timedelta(days=1)))
        for detail, expected in [('HTTP 429 rate limit', 'rate_limit'),
                                 ('certificate verify failed', 'certificate'),
                                 ('invalid model response', 'model_format')]:
            with self.subTest(detail=detail):
                error = RuntimeError(f'{self.client} did not complete plan: {detail}')
                self.assertEqual(expected, failures.category(error))


class DSHModelServiceRecoveryTests(ModelServiceRecoveryTests):
    channel = 'dsh'
    client = 'DSH'

    def test_dsh_recovery_preserves_legacy_opencode_receipts(self):
        self.call('legacy-opencode', 'legacy-task', '2026-11-01T01:00:00+00:00',
                  '2026-11-01T01:02:00+00:00', 'success', channel='opencode')
        calls = list(self.db.execute('SELECT * FROM model_calls ORDER BY call_id'))
        self.assertTrue(self.recover())
        self.assertEqual(calls, list(self.db.execute('SELECT * FROM model_calls ORDER BY call_id')))
        receipt = self.data['model_service_recoveries'][0]
        self.assertEqual('dsh', receipt['failed_call']['channel'])
        self.assertEqual('dsh', receipt['success_call']['channel'])

    def test_incomplete_or_malformed_stream_uses_format_budget_not_patch_attempts(self):
        from dsh_adapter import DSHProtocolError, parse_result
        for name, output in (
                ('missing_final', json.dumps({'type': 'session', 'sessionId': 'fixture'})),
                ('malformed_json', '{"type":"session"')):
            with self.subTest(case=name):
                with self.assertRaises(DSHProtocolError) as caught:
                    parse_result(output, 0)
                data = {'stage': 'PLAN', 'attempts': {'PLAN': 1}}
                for _ in range(3):
                    self.assertTrue(failures.record_failure(data, 'PLAN', caught.exception, {}, NOW))
                self.assertEqual('model_format', data['failure_category'])
                self.assertEqual({'PLAN': 1}, data['attempts'])
                self.assertEqual(3, data['infrastructure_failures']['PLAN']['count'])
                self.assertFalse(failures.retry_ready(data, 'PLAN', NOW + dt.timedelta(days=1)))
                before = copy.deepcopy(data)
                # Even with the matching DSH service-success receipts from
                # setUp, protocol failures cannot reopen a service window.
                self.assertFalse(self.recover(data))
                self.assertEqual(before, data)


if __name__ == '__main__':
    unittest.main()
