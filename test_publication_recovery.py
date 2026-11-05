"""Publication recovery with real temporary Git/tests and fake GitHub only."""
import copy
import datetime as dt
import json
import unittest
from unittest.mock import patch

from contribute import ContributionPaused
from scheduling import RunTaskBudget
import test_delivery_integration as execution_fixture


class PublicationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fx = execution_fixture.DeliveryIntegrationTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.addCleanup(self.fx.tearDown)

    def receipt(self):
        return self.fx.root / 'runs' / self.fx.key / 'verification.json'

    def expire(self):
        evidence = json.loads(self.receipt().read_text(encoding='utf-8'))
        evidence['at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat()
        self.receipt().write_text(json.dumps(evidence), encoding='utf-8')
        return evidence

    def pending(self):
        data = self.fx.prepared()
        self.fx.lose_pr_response = True
        with self.fx.mocks():
            with self.assertRaisesRegex(RuntimeError, 'Network lost'):
                self.fx.engine.publish_pr(self.fx.key, data)
        self.assertTrue(data['pr_create_pending'])
        self.assertEqual(1, len(self.fx.created_prs))
        return self.fx.engine.load(self.fx.key)

    def test_expired_receipt_returns_to_real_red_green_and_review(self):
        fx = self.fx
        data = fx.prepared()
        before = self.expire()
        fingerprint = data['approved_fingerprint']
        with fx.mocks():
            fx.engine.publish_pr(fx.key, data)
        self.assertEqual('PATCH_READY', data['stage'])
        self.assertNotIn('approved_fingerprint', data)
        self.assertEqual(before, data['publication_revalidations'][-1]['verification'])
        self.assertEqual([], fx.created_prs)
        with fx.mocks():
            fx.engine.verify(fx.key, data)
            current = json.loads(self.receipt().read_text(encoding='utf-8'))
            self.assertNotEqual(before['logs'], current['logs'])
            self.assertEqual([1, 0, 0], [item['exit_code'] for item in current['results']])
            self.assertNotIn('approved_fingerprint', data)
            fx.engine.review(fx.key, data)
        self.assertEqual('PUBLISH', data['stage'])
        self.assertEqual(fingerprint, data['approved_fingerprint'])
        self.assertEqual([], fx.created_prs)

    def test_changed_patch_is_reverified_instead_of_exhausting_publish_retries(self):
        fx = self.fx
        data = fx.prepared()
        original = data['approved_fingerprint']
        (fx.work / 'value.py').write_text('VALUE = 2  # retained clarification\n', encoding='utf-8')
        with fx.mocks():
            fx.engine.publish_pr(fx.key, data)
            self.assertEqual('PATCH_READY', data['stage'])
            fx.engine.verify(fx.key, data)
            fx.engine.review(fx.key, data)
        self.assertEqual('PUBLISH', data['stage'])
        self.assertNotEqual(original, data['approved_fingerprint'])
        self.assertEqual({}, data['attempts'])
        self.assertEqual([], fx.created_prs)

    def test_mismatched_baseline_reruns_baseline_and_failed_new_review_cannot_publish(self):
        fx = self.fx
        data = fx.prepared()
        evidence = json.loads(self.receipt().read_text(encoding='utf-8'))
        evidence['base_sha'] = 'wrong-old-baseline'
        self.receipt().write_text(json.dumps(evidence), encoding='utf-8')
        with fx.mocks():
            fx.engine.publish_pr(fx.key, data)
            self.assertEqual('PATCH_READY', data['stage'])
            fx.engine.verify(fx.key, data)
            current = json.loads(self.receipt().read_text(encoding='utf-8'))
            self.assertEqual(fx.base, current['base_sha'])
            self.assertEqual(1, current['results'][0]['exit_code'])
            with patch.object(fx.engine, 'judge', return_value={'approved': False, 'reason': 'Deliberately rejected fresh review'}):
                with self.assertRaisesRegex(RuntimeError, 'Patch review rejected'):
                    fx.engine.review(fx.key, data)
        self.assertNotIn('approved_fingerprint', data)
        self.assertEqual([], fx.created_prs)

    def test_revalidation_preserves_same_tree_commit_intent_after_lost_commit_response(self):
        fx = self.fx
        data = fx.prepared()
        def lost_commit(argv, **kwargs):
            result = fx.fake_command(argv, **kwargs)
            if argv[:2] == ['git', 'commit']:
                raise RuntimeError('commit response lost')
            return result
        with fx.mocks(), patch('autonomous.command', side_effect=lost_commit):
            with self.assertRaisesRegex(RuntimeError, 'commit response lost'):
                fx.engine.publish_pr(fx.key, data)
        head, tree = fx.git('rev-parse', 'HEAD'), data['expected_tree']
        self.expire()
        with fx.mocks():
            fx.engine.publish_pr(fx.key, data)
            self.assertEqual(tree, data['expected_tree'])
            fx.engine.verify(fx.key, data)
            fx.engine.review(fx.key, data)
            self.assertEqual(tree, data['expected_tree'])
            fx.engine.publish_pr(fx.key, data)
        self.assertEqual(head, fx.git('rev-parse', 'HEAD'))
        self.assertEqual('PR_OPEN', data['stage'])
        self.assertEqual(1, len(fx.created_prs))

    def test_pending_pr_recovers_read_only_despite_expiry_policy_and_terminal_task(self):
        fx = self.fx
        data = self.pending()
        self.expire()
        data['stage'] = 'FAILED'
        data['publication_hold'] = 'external_writes_disabled'
        data['infrastructure_failures'] = {'PUBLISH': {'exhausted': True}}
        fx.engine.store(fx.key, data, 'FAILED')
        fx.p.allow_external_writes = False
        with fx.mocks(), patch.object(fx.p, 'effective_repositories', return_value=[]), \
                patch.object(fx.p, 'require_contribution', side_effect=ContributionPaused('Repository now requires personal submission')) as policy, \
                patch.object(fx.p, 'fingerprint', side_effect=AssertionError('Recovery must not need patch verification')), \
                patch.object(fx.engine, 'model', side_effect=AssertionError('Recovery must not call a model')), \
                patch.object(fx.engine, 'feedback', side_effect=AssertionError('Recovery must stop after registration')), \
                patch('autonomous.command', side_effect=AssertionError('Recovery must not push, edit, fork or link')):
            rows = fx.engine.candidates()
            self.assertIn(fx.key, [row['id'] for row in rows])
            budget = RunTaskBudget(fx.p.config)
            fx.engine.dispatch_candidates(rows, budget, set())
        saved = fx.engine.load(fx.key)
        self.assertEqual('PR_OPEN', saved['stage'])
        self.assertEqual(3, saved['pr_number'])
        self.assertFalse(saved['pr_create_pending'])
        self.assertTrue(saved['pr_reconciliation']['exact_head_verified'])
        self.assertEqual({fx.key}, budget.used['maintenance'])
        policy.assert_not_called()
        self.assertEqual(1, len(fx.created_prs))

    def test_pending_pr_wrong_exact_identity_requires_manual_verification(self):
        fx = self.fx
        original = self.pending()
        for field in ('sha', 'target', 'fork', 'branch', 'author'):
            data = copy.deepcopy(original)
            wrong = copy.deepcopy(fx.created_prs[0])
            if field == 'sha':
                wrong['head']['sha'] = '0' * 40
            elif field == 'target':
                wrong['base']['repo']['full_name'] = 'other/repo'
            elif field == 'fork':
                wrong['head']['repo']['full_name'] = 'someone/repo'
            elif field == 'branch':
                wrong['head']['ref'] = 'different-branch'
            else:
                wrong['user']['login'] = 'someone'
            def response(*args):
                return wrong if args == ('api', 'repos/owner/repo/pulls/3') else fx.fake_gh(*args)
            with self.subTest(field=field), fx.mocks(), patch('autonomous.gh', side_effect=response), \
                    patch('autonomous.command', side_effect=AssertionError('No external action during recovery')):
                with self.assertRaisesRegex(ContributionPaused, 'exact head'):
                    fx.engine.publish_pr(fx.key, data)
            self.assertTrue(data['pr_create_pending'])
            self.assertNotIn('pr_number', data)
            self.assertEqual('manual_verification', data['pr_reconciliation']['state'])
        self.assertEqual(1, len(fx.created_prs))

    def test_policy_gate_is_preserved_for_new_writes_and_revalidation(self):
        fx = self.fx
        data = fx.prepared()
        self.expire()
        with fx.mocks(), patch.object(fx.p, 'require_contribution', side_effect=ContributionPaused('Required ready label missing')):
            with self.assertRaisesRegex(ContributionPaused, 'ready label'):
                fx.engine.publish_pr(fx.key, data)
        self.assertEqual('PUBLISH', data['stage'])
        self.assertEqual([], fx.created_prs)


if __name__ == '__main__':
    unittest.main()
