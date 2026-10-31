import copy
import hashlib
import json
import unittest

from feature_matrix import build, route
from research import validate
from test_feature_matrix import matrix_answer
from test_research import opportunity


def evidence_id(repo, sha, path, text):
    identity = json.dumps([repo, sha, path, hashlib.sha256(text.encode()).hexdigest()], separators=(',', ':'))
    return 'E' + hashlib.sha256(identity.encode()).hexdigest()[:16]


class CompactResearchEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.task = {'repo': 'owner/repo', 'base_sha': 'a' * 40, 'workspace': '.'}
        self.peers = ['peer/one', 'peer/two', 'peer/three']
        self.packet = {'schema_version': 2, 'policy': {'CONTRIBUTING.md': 'Small bug fixes welcome.'},
                       'target': {'repo': 'owner/repo', 'sha': 'a' * 40,
                                  'files': {'value.py': 'VALUE = 1', 'CONTRIBUTING.md': 'Small bug fixes welcome.'}},
                       'peers': [{'repo': repo, 'sha': str(i) * 40, 'files': {'feature.py': 'VALUE = 2'}}
                                 for i, repo in enumerate(self.peers, 1)]}

    def compact(self, item):
        source = next(source for source in [self.packet['target'], *self.packet['peers']] if source['repo'] == item['repo'])
        return {'evidence_id': evidence_id(item['repo'], source['sha'], item['path'], source['files'][item['path']]),
                'quote': item['quote']}

    def test_controller_evidence_reference_expands_then_checks_exact_quote(self):
        answer = opportunity()
        for group in ('evidence', 'policy_evidence'):
            answer[group] = [self.compact(item) for item in answer[group]]
        validate(answer, self.task, self.packet)
        self.assertEqual('value.py', answer['evidence'][0]['path'])
        self.assertEqual('a' * 40, answer['evidence'][0]['sha'])
        self.assertEqual('VALUE = 1', answer['evidence'][0]['quote'])
        self.assertIn('blob/' + 'a' * 40, answer['evidence'][0]['url'])

    def test_matrix_references_keep_peer_scope_and_exact_source_validation(self):
        answer = matrix_answer(self.task, self.peers)
        for status in answer['feature_matrix']['feature_status'].values():
            status['evidence'] = [self.compact(item) for item in status['evidence']]
        matrix = build(answer, self.task, self.packet)
        self.assertEqual(self.peers, [item['repo'] for item in matrix['evidence']])
        self.assertTrue(all(item['sha'] and item['quote'] == 'VALUE = 2' for item in matrix['evidence']))

    def test_reference_never_approves_unknown_stale_or_conflicting_evidence(self):
        original = opportunity()
        compact = self.compact(original['evidence'][0])
        cases = [{**compact, 'evidence_id': 'E0000000000000000'},
                 {**compact, 'repo': 'peer/one'},
                 {**compact, 'path': 'unread.py'},
                 {**compact, 'sha': 'b' * 40},
                 {**compact, 'quote': 'VALUE = 2'}]
        for item in cases:
            answer = copy.deepcopy(original)
            answer['evidence'] = [item]
            with self.subTest(item=item), self.assertRaises(ValueError):
                validate(answer, self.task, self.packet)
        answer = copy.deepcopy(original)
        answer['evidence'] = [compact]
        self.packet['target']['files']['value.py'] = 'VALUE = 2'
        with self.assertRaises(ValueError):
            validate(answer, self.task, self.packet)

    def test_deliverable_bug_precedes_unconfirmed_feature_without_changing_admission(self):
        bug = opportunity()
        feature = {**bug, 'category': 'feature'}
        approved = {'reviewed': True, 'duplicate_clear': True}
        self.assertEqual('READY_TO_IMPLEMENT', route(bug, None, **approved)[0])
        self.assertEqual('NEEDS_MAINTAINER_CONFIRMATION', route(feature, None, **approved)[0])
        self.assertGreater(route(bug, None, **approved)[1], route(feature, None, **approved)[1])
        self.assertEqual(route(bug, None, **approved)[1], route(feature, None, maintainer=True, **approved)[1])
        self.assertEqual(0, route(feature, None)[1])


if __name__ == '__main__':
    unittest.main()
