"""Feature discovery/admission tests use only in-memory observations."""
import copy
import unittest

from feature_requests import candidate_metadata, constraints, evaluate, is_feature_request, scope_digest
from repository_policy import approval_receipt


NOW = '2026-11-05T06:00:00+00:00'


def comment(number, author, body, association='NONE'):
    return {'id': number, 'user': {'login': author, 'type': 'User'}, 'body': body,
            'author_association': association, 'created_at': f'2026-11-05T05:00:{number:02d}Z',
            'html_url': f'https://github.com/org/project/issues/12#issuecomment-{number}'}


def snapshot():
    return {'repo': 'org/project', 'observed_at': NOW,
            'issue': {'number': 12, 'title': 'Feature request: add export', 'body': 'Export the current document.',
                      'state': 'open', 'labels': [{'name': 'enhancement'}], 'assignees': [],
                      'html_url': 'https://github.com/org/project/issues/12'},
            'comments': [], 'linked_prs': [], 'policy_rules': 'Contributions welcome.',
            'complete': {'issue': True, 'comments': True, 'linked_prs': True, 'policy': True}}


class ClassificationTests(unittest.TestCase):
    def test_explicit_types_and_prefixes(self):
        for label in ('feature', 'Feature Request', 'enhancement', 'type:feature', 'type: enhancement', 'kind/feature'):
            with self.subTest(label=label):
                self.assertTrue(is_feature_request({'title': 'Add export', 'labels': [{'name': label}]}))
        for title in ('[Feature] Export', '[feature request]: export', 'Enhancement: export', 'feat: export', 'feat(ui): export'):
            with self.subTest(title=title):
                self.assertTrue(is_feature_request({'title': title}))

    def test_component_labels_and_body_keywords_are_not_feature_types(self):
        for label in ('feature:st.App', 'feature:st.dataframe', 'feature-requested-by-bot', 'bug', 'help wanted', 'good first issue'):
            with self.subTest(label=label):
                issue = {'title': 'App crashes', 'body': 'Feature flags crash', 'labels': [{'name': label}]}
                self.assertFalse(is_feature_request(issue))
        issue = snapshot()['issue']
        issue['pull_request'] = {'url': 'api'}
        self.assertFalse(is_feature_request(issue))

    def test_metadata_is_not_permission(self):
        issue = snapshot()['issue']
        result = candidate_metadata(issue)
        self.assertEqual(('feature', 'unassigned', 'not_assessed'),
                         (result['kind'], result['assignment'], result['authorization']))
        self.assertNotIn('maintainer_approved', result)
        del issue['assignees']
        self.assertEqual('unknown', candidate_metadata(issue)['assignment'])


class AdmissionTests(unittest.TestCase):
    def assess(self, saved=None, policy=None, **kwargs):
        return evaluate(saved or snapshot(), policy, actor='our-user', now=NOW, **kwargs)

    def test_unassigned_feature_can_plan_without_fabricated_approval(self):
        saved = snapshot()
        saved['issue']['labels'].append({'name': 'help wanted'})
        saved['comments'] = [comment(1, 'requester', 'This would be very useful!')]
        before = copy.deepcopy(saved)
        result = self.assess(saved)
        self.assertEqual('eligible', result['status'])
        self.assertFalse(result['scope_approval_current'])
        self.assertFalse(result['permits_publication'])
        self.assertEqual(before, saved)

    def test_assignment_and_current_issue_identity(self):
        saved = snapshot()
        saved['issue']['assignees'] = [{'login': 'someone-else'}]
        self.assertEqual('blocked', self.assess(saved)['status'])
        saved['issue']['assignees'] = [{'login': 'OUR-USER'}]
        self.assertEqual('eligible', self.assess(saved)['status'])
        saved['issue']['html_url'] = 'https://github.com/other/project/issues/12'
        self.assertEqual('unknown', self.assess(saved)['status'])
        saved['issue']['state'] = 'closed'
        self.assertEqual('blocked', self.assess(saved)['status'])

    def test_incomplete_stale_future_or_malformed_evidence_never_clears(self):
        for key in ('issue', 'comments', 'linked_prs', 'policy'):
            saved = snapshot()
            saved['complete'][key] = False
            self.assertEqual('unknown', self.assess(saved)['status'])
        for at in ('2026-11-05T05:44:59Z', '2026-11-05T06:00:01Z', 'bad', '2026-11-05T06:00:00'):
            saved = snapshot()
            saved['observed_at'] = at
            self.assertEqual('unknown', self.assess(saved)['status'])
        for key, value in [('comments', [{'body': 'I am working on this'}]), ('linked_prs', None)]:
            saved = snapshot()
            saved[key] = value
            self.assertEqual('unknown', self.assess(saved)['status'])
        for field in ('labels', 'assignees'):
            saved = snapshot()
            del saved['issue'][field]
            self.assertEqual('unknown', self.assess(saved)['status'])

    def test_claims_do_not_expire_and_release_is_author_specific(self):
        saved = snapshot()
        old = comment(1, 'other', 'I am working on this.')
        old['created_at'] = '2020-01-01T00:00:00Z'
        saved['comments'] = [old, comment(2, 'unrelated', 'Anyone can take this.')]
        self.assertEqual('blocked', self.assess(saved)['status'])
        saved['comments'].append(comment(3, 'other', "I'm no longer working on this."))
        self.assertEqual('eligible', self.assess(saved)['status'])

    def test_interest_blocks_but_quotes_and_bots_do_not_claim(self):
        saved = snapshot()
        saved['comments'] = [comment(1, 'other', 'Could I work on this?')]
        self.assertEqual('blocked', self.assess(saved)['status'])
        saved['comments'] = [comment(1, 'other', '> I am working on this\nAn example above.'),
                             comment(2, 'other', '```\nI am working on this\n```')]
        saved['comments'].append({**comment(3, 'bot', 'I am working on this'), 'user': {'login': 'bot', 'type': 'Bot'}})
        self.assertEqual('eligible', self.assess(saved)['status'])

    def test_claim_and_assignment_requirements_cannot_be_invented(self):
        saved = snapshot()
        self.assertEqual('blocked', self.assess(saved, {'claim_required': True})['status'])
        saved['comments'] = [comment(1, 'our-user', 'I will work on this.')]
        self.assertEqual('eligible', self.assess(saved, {'claim_required': True})['status'])
        self.assertEqual('blocked', self.assess(saved, {'assignment_required': True})['status'])
        saved['issue']['assignees'] = [{'login': 'our-user'}]
        self.assertEqual('eligible', self.assess(saved, {'assignment_required': True})['status'])

    def test_open_and_merged_prs_require_review_closed_unmerged_does_not_reserve(self):
        saved = snapshot()
        for state in ('open', 'OPEN', 'merged'):
            saved['linked_prs'] = [{'number': 23, 'state': state}]
            self.assertEqual('blocked', self.assess(saved)['status'])
        saved['linked_prs'] = [{'number': 23, 'state': 'closed', 'merged_at': NOW}]
        self.assertEqual('blocked', self.assess(saved)['status'])
        saved['linked_prs'] = [{'number': 23, 'state': 'closed', 'merged': False}]
        self.assertEqual('eligible', self.assess(saved)['status'])
        saved['linked_prs'] = [{'number': 23}]
        self.assertEqual('unknown', self.assess(saved)['status'])

    def test_ready_and_human_review_rules_are_not_scope_approval(self):
        saved = snapshot()
        policy = {'required_issue_labels': ['ready'], 'ready_project': 'https://github.com/orgs/org/projects/1'}
        self.assertEqual('blocked', self.assess(saved, policy)['status'])
        saved['issue']['labels'].append({'name': 'ready'})
        saved['ready_project_verified'] = True
        result = self.assess(saved, policy)
        self.assertEqual('eligible', result['status'])
        self.assertFalse(result['scope_approval_current'])
        self.assertEqual('eligible', self.assess(saved, {'human_pr_review_required': True})['status'])
        self.assertEqual('blocked', self.assess(saved, {'human_pr_review_required': True}, stage='publish')['status'])

    def test_only_matching_current_human_comment_receipt_satisfies_scope_gate(self):
        saved = snapshot()
        policy = {'feature_approval_required': True}
        human = comment(1, 'maintainer', 'Please implement exactly this export scope.', 'MEMBER')
        saved['comments'] = [human]
        data = {'maintainer_approval': approval_receipt([human], {'approved': True}, NOW),
                'feature_request_scope_digest': scope_digest(saved)}
        result = self.assess(saved, policy, approval_data=data)
        self.assertEqual('eligible', result['status'])
        self.assertTrue(result['scope_approval_current'])
        saved['comments'].append(comment(2, 'maintainer', 'Please wait; the scope needs revision.', 'MEMBER'))
        self.assertEqual('blocked', self.assess(saved, policy, approval_data=data)['status'])
        self.assertEqual('blocked', self.assess(saved, policy, approval_data={'maintainer_approved': True})['status'])
        saved['comments'] = [{**human, 'author_association': 'CONTRIBUTOR'}]
        self.assertEqual('blocked', self.assess(saved, policy, approval_data=data)['status'])

    def test_approval_must_bind_current_issue_scope_not_metadata_timestamps(self):
        saved = snapshot()
        human = comment(1, 'maintainer', 'Please implement this.', 'OWNER')
        saved['comments'] = [human]
        data = {'maintainer_approval': approval_receipt([human], {'approved': True}, NOW),
                'feature_request_scope_digest': scope_digest(saved)}
        policy = {'feature_approval_required': True}
        saved['issue']['updated_at'] = '2026-11-05T05:59:59Z'
        self.assertEqual('eligible', self.assess(saved, policy, approval_data=data)['status'])
        for field in ('title', 'body'):
            changed = copy.deepcopy(saved)
            changed['issue'][field] += ' Also change all authentication.'
            self.assertEqual('blocked', self.assess(changed, policy, approval_data=data)['status'])
        del data['feature_request_scope_digest']
        self.assertEqual('blocked', self.assess(saved, policy, approval_data=data)['status'])

    def test_manual_pr_delivery_blocks_all_stages_manual_issue_does_not_block_existing_issue(self):
        for stage in ('plan', 'repair', 'publish'):
            self.assertEqual('blocked', self.assess(policy={'manual_pr_submission': True}, stage=stage)['status'])
        self.assertEqual('eligible', self.assess(policy={'manual_issue_submission': True})['status'])

    def test_automation_ban_is_not_overridden_by_scope_approval(self):
        saved = snapshot()
        human = comment(1, 'maintainer', 'Please implement this.', 'OWNER')
        saved['comments'] = [human]
        data = {'maintainer_approval': approval_receipt([human], {'approved': True}, NOW)}
        self.assertEqual('blocked', self.assess(saved, {'automated_pr': False}, approval_data=data)['status'])


class RuleTests(unittest.TestCase):
    def test_verified_qwen_and_next_feature_requirements(self):
        qwen = 'For features: The PR should be linked to the feature request or proposal issue that has been approved by a maintainer.'
        nextjs = 'You do not need permission to work on an issue. For new features, open a discussion and get the proposal accepted first.'
        template = 'Implements an existing feature request or RFC. Make sure the feature request has been accepted for implementation before opening a PR.'
        for rules in (qwen, nextjs, template):
            with self.subTest(rules=rules):
                self.assertTrue(constraints(rules)['feature_approval_required'])
                saved = snapshot()
                saved['policy_rules'] = rules
                self.assertEqual('blocked', evaluate(saved, actor='our-user', now=NOW)['status'])
        self.assertTrue(constraints(nextjs)['discussion_required'])

    def test_explicit_claim_assignment_and_discussion_requirements(self):
        cases = [('Please claim an issue before starting work.', 'claim_required'),
                 ('Before working on an issue, please claim it.', 'claim_required'),
                 ('You must be assigned to the issue before coding.', 'assignment_required'),
                 ('Before starting work, make sure the issue is assigned to you.', 'assignment_required'),
                 ('Please discuss the feature before implementation.', 'discussion_required')]
        for rules, key in cases:
            with self.subTest(rules=rules):
                self.assertTrue(constraints(rules)[key])

    def test_optional_prose_and_labels_do_not_create_approval(self):
        result = constraints('Contributions welcome. Features are useful. Use help wanted labels when convenient.',
                             {'claim_required': True, 'required_issue_labels': ['ready']})
        self.assertTrue(result['claim_required'])
        self.assertEqual(['ready'], result['required_issue_labels'])
        self.assertNotIn('feature_approval_required', result)

    def test_explicit_no_approval_rule_does_not_become_a_requirement(self):
        for rules in ('For features, you do not need approval.',
                      'For features, maintainer approval is not required before coding.',
                      'No maintainer approval is required before implementation.'):
            with self.subTest(rules=rules):
                self.assertNotIn('feature_approval_required', constraints(rules))
        self.assertTrue(constraints('For bugs, you do not need approval, but for features you must get approval.')['feature_approval_required'])


if __name__ == '__main__':
    unittest.main()
