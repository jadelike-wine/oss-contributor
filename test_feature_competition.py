"""Linked PR scope decisions use complete fake GET evidence and temporary SQLite."""
import copy
import unittest
from unittest.mock import patch

from contribute import ContributionPaused
from feature_requests import evaluate, linked_pr_identity, linked_pr_review_key
import test_feature_request_flow as flow_fixture
import test_feature_requests as pure_fixture


def pr(repo='owner/repo', number=44, *, merged=False, author='other'):
    return {'number': number, 'html_url': f'https://github.com/{repo}/pull/{number}',
            'state': 'closed' if merged else 'open', 'merged': merged,
            'title': 'Document export support', 'body': 'Export one current document without empty documents.',
            'base': {'repo': {'full_name': repo}}, 'head': {'sha': 'a' * 40},
            'user': {'login': author, 'type': 'User'}}


def decision(issue, linked, relation, **extra):
    return {'url': linked['html_url'], 'head_sha': linked['head']['sha'], 'relation': relation,
            'reason': 'Compare the exact requested behavior with the inspected implementation.',
            'evidence': [{'source': 'issue', 'quote': issue['body']},
                         {'source': 'pr', 'quote': linked['body']}], **extra}


class LinkedPrEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.s = pure_fixture.snapshot()
        self.s['repo'] = 'owner/repo'
        self.s['issue']['html_url'] = 'https://github.com/owner/repo/issues/12'
        self.s['linked_prs'] = [pr()]

    def assess(self, relation=None, *, own=False, **extra):
        data = {}
        if relation:
            data['feature_linked_pr_review'] = {
                'key': linked_pr_review_key(self.s, own), 'approved': True, 'reason': 'Explicit fixture verdict.',
                'decisions': [decision(self.s['issue'], self.s['linked_prs'][0], relation, **extra)]}
        return evaluate(self.s, actor='our-user', now=pure_fixture.NOW,
                        approval_data=data, own_competition=own)

    def test_reference_alone_is_unknown_pending_scope_review(self):
        result = self.assess()
        self.assertEqual('unknown', result['status'])
        self.assertEqual(1, len(result['linked_pr_review_pending']))
        self.assertEqual([], result['unknown_reasons'])

    def test_cross_repository_merged_downstream_is_not_target_implementation(self):
        self.s['linked_prs'] = [pr('downstream/client', merged=True)]
        self.assertEqual('eligible', self.assess('downstream')['status'])
        self.assertEqual('unknown', self.assess('implemented')['status'])

    def test_target_merged_coverage_blocks_even_own_alternative(self):
        self.s['linked_prs'] = [pr(merged=True)]
        result = self.assess('implemented', own=True)
        self.assertEqual('blocked', result['status'])
        self.assertIn('implements the requested scope', result['reasons'][0])

    def test_real_competitor_is_allowed_only_with_own_alternative_preference(self):
        self.assertEqual('blocked', self.assess('competing')['status'])
        self.assertEqual('eligible', self.assess('competing', own=True)['status'])

    def test_partial_coverage_must_name_the_remaining_scope(self):
        self.assertEqual('unknown', self.assess('partial')['status'])
        self.assertEqual('eligible', self.assess('partial', uncovered_scope='Export empty documents.')['status'])
        self.assertEqual('unknown', self.assess('downstream')['status'])

    def test_identity_must_bind_base_url_number_and_full_head_sha(self):
        original = self.s['linked_prs'][0]
        mutations = [('base', {'repo': {'full_name': 'another/repo'}}), ('base', 'malformed'),
                     ('head', {'sha': 'abc'}), ('html_url', 'https://example.org/owner/repo/pull/44')]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                self.s['linked_prs'] = [{**original, field: value}]
                self.assertIsNone(linked_pr_identity(self.s['linked_prs'][0]))
                self.assertEqual('unknown', self.assess()['status'])

    def test_exact_both_body_quotes_are_required_and_title_only_is_not_coverage(self):
        for evidence in ([{'source': 'pr', 'quote': self.s['linked_prs'][0]['title']}],
                         [{'source': 'issue', 'quote': self.s['issue']['body']},
                          {'source': 'pr', 'quote': 'Invented implementation evidence.'}]):
            with self.subTest(evidence=evidence):
                self.assertEqual('unknown', self.assess('unrelated', evidence=evidence)['status'])

    def test_receipt_does_not_survive_new_head_or_issue_scope(self):
        linked = self.s['linked_prs'][0]
        data = {'feature_linked_pr_review': {'key': linked_pr_review_key(self.s), 'approved': True,
                'decisions': [decision(self.s['issue'], linked, 'unrelated')]}}
        for subject, field, value in ((linked['head'], 'sha', 'b' * 40), (self.s['issue'], 'body', 'Different scope')):
            old = subject[field]
            subject[field] = value
            result = evaluate(self.s, actor='our-user', now=pure_fixture.NOW, approval_data=data)
            self.assertEqual('unknown', result['status'])
            self.assertTrue(result['linked_pr_review_pending'])
            subject[field] = old


class LinkedPrFlowTests(unittest.TestCase):
    def setUp(self):
        # Composition avoids duplicate discovery of the shared TestCase fixture.
        self.fx = flow_fixture.FeatureRequestFlowTests(methodName='runTest')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.linked = [pr('downstream/client', merged=True)]
        self.relations = ['downstream']
        self.review_calls = []
        self.bad_decisions = None
        review = patch.object(self.fx.engine, 'judge', side_effect=self.review)
        review.start()
        self.addCleanup(review.stop)
        self.install_links()

    def install_links(self):
        self.fx.responses[flow_fixture.BASE + '/timeline?per_page=100&page=1'] = [
            flow_fixture.reference(p['number'], p['base']['repo']['full_name']) for p in self.linked]
        for p in self.linked:
            self.fx.responses[f"repos/{p['base']['repo']['full_name']}/pulls/{p['number']}"] = p

    def review(self, key, purpose, packet):
        self.review_calls.append((purpose, copy.deepcopy(packet)))
        result = {'approved': True, 'reason': 'Explicit fake decision from complete fixture evidence.'}
        if purpose == 'feature-linked-pr-review':
            result['decisions'] = (self.bad_decisions if self.bad_decisions is not None else
                [decision(self.fx.issue, p, r) for p, r in zip(self.linked, self.relations)])
        return result

    def own_issue(self):
        self.fx.issue['user'] = {'login': 'our-user', 'type': 'User'}
        self.fx.p.config['contribution_strategy'] = {'prefer_own_pr_on_competition': True}

    def competitor(self, claim=True):
        self.linked = [pr()]
        self.relations = ['competing']
        self.install_links()
        if claim:
            self.fx.responses[flow_fixture.BASE + '/comments?per_page=100&page=1'] = [
                flow_fixture.comment(1, 'I have opened a PR for this.', 'other')]

    def test_one_bounded_batch_review_then_normal_feature_review_and_cached_reuse(self):
        self.linked.append(pr('second/client', 45))
        self.relations.append('unrelated')
        self.install_links()
        self.fx.check()
        self.fx.check()
        self.assertEqual(['feature-linked-pr-review', 'feature-request-review'], [x[0] for x in self.review_calls])
        self.assertEqual(2, len(self.review_calls[0][1]['linked_prs']))
        saved = self.fx.engine.load(self.fx.key)
        self.assertEqual(2, len(saved['feature_linked_pr_review']['decisions']))
        self.assertNotIn('maintainer_approval', saved)

    def test_foreign_identity_mismatch_remains_unknown_without_model(self):
        self.linked[0]['base']['repo']['full_name'] = 'untrusted/other'
        with self.assertRaisesRegex(ContributionPaused, 'Incomplete linked_prs'):
            self.fx.check()
        self.assertEqual([], self.review_calls)

    def test_own_issue_pr_submission_claim_reaches_competition_review(self):
        self.own_issue()
        self.competitor()
        self.fx.check()
        self.assertEqual(['feature-linked-pr-review', 'feature-request-review'], [x[0] for x in self.review_calls])
        self.assertTrue(self.review_calls[0][1]['own_competition'])

    def test_preference_does_not_apply_to_someone_elses_issue(self):
        self.own_issue()
        self.fx.issue['user']['login'] = 'someone-else'
        self.competitor(claim=False)
        with self.assertRaisesRegex(ContributionPaused, 'Existing open linked PR'):
            self.fx.check()
        self.assertEqual(['feature-linked-pr-review'], [x[0] for x in self.review_calls])
        self.assertFalse(self.review_calls[0][1]['own_competition'])

    def test_own_competition_never_overrides_assignee_or_required_claim(self):
        self.own_issue()
        self.competitor()
        self.fx.issue['assignees'] = [{'login': 'other'}]
        with self.assertRaisesRegex(ContributionPaused, 'assigned'):
            self.fx.check()
        self.fx.issue['assignees'] = []
        self.fx.rules = 'Please claim an issue before starting work.'
        with self.assertRaisesRegex(ContributionPaused, 'prior claim'):
            self.fx.check()
        self.assertEqual([], self.review_calls)

    def test_unverified_or_active_work_claim_is_not_a_competitor_exception(self):
        self.own_issue()
        self.competitor()
        for body in ('I am working on this.', 'I have opened a PR for this.'):
            self.fx.responses[flow_fixture.BASE + '/comments?per_page=100&page=1'] = [
                flow_fixture.comment(1, body, 'another-author')]
            with self.assertRaisesRegex(ContributionPaused, 'unresolved claim'):
                self.fx.check()
        self.assertEqual([], self.review_calls)

    def test_missing_per_pr_decision_cannot_be_general_approval(self):
        self.bad_decisions = []
        with self.assertRaisesRegex(ValueError, 'Invalid reviewer verdict'):
            self.fx.check()
        self.assertEqual(['feature-linked-pr-review'], [x[0] for x in self.review_calls])
        self.assertNotIn('feature_linked_pr_review', self.fx.engine.load(self.fx.key))
        self.bad_decisions = None
        self.fx.check()
        self.assertEqual(2, sum(p == 'feature-linked-pr-review' for p, _ in self.review_calls))

    def test_fabricated_body_quote_is_not_cached_as_a_permanent_unknown(self):
        self.bad_decisions = [decision(self.fx.issue, self.linked[0], 'downstream')]
        self.bad_decisions[0]['evidence'][0]['quote'] = 'Not present in the source body.'
        with self.assertRaisesRegex(ValueError, 'Invalid reviewer verdict'):
            self.fx.check()
        self.assertNotIn('feature_linked_pr_review', self.fx.engine.load(self.fx.key))

    def test_new_head_requires_new_review_instead_of_reusing_positive_receipt(self):
        self.fx.check()
        self.linked[0]['head']['sha'] = 'b' * 40
        self.fx.check()
        self.assertEqual(2, sum(p == 'feature-linked-pr-review' for p, _ in self.review_calls))


if __name__ == '__main__':
    unittest.main()
