import copy
import unittest

from feature_matrix import build, route


class LocalFixResearchTests(unittest.TestCase):
    def fixture(self, category='bug', peers=()):
        task = {'repo': 'owner/repo', 'base_sha': 'a' * 40}
        source = 'def read_path(path):\n    path.mkdir(exist_ok=True)\n    return path.read_text()\n'
        target = {'status': 'partial', 'reason': 'Read creates a directory.',
                  'missing': 'A read must leave a missing path absent.',
                  'checked_paths': ['src/storage.py'],
                  'evidence': [{'repo': task['repo'], 'path': 'src/storage.py',
                                'quote': 'path.mkdir(exist_ok=True)'}]}
        packet = {'target': {'repo': task['repo'], 'sha': task['base_sha'],
                             'files': {'src/storage.py': source, 'README.md': 'Read files.'}},
                  'peers': [{'repo': peer, 'sha': 'b' * 40, 'files': {'src/reader.py': 'pass'}}
                            for peer in peers]}
        answer = {'decision': 'candidate', 'category': category, 'risk': 'low',
                  'policy': {'automated_issue': True, 'automated_pr': True,
                             'maintainer_required': False},
                  'feature_matrix': {
                      'feature_key': 'read-without-creating-directories',
                      'feature_name': 'Side-effect-free read', 'category': 'tools',
                      'user_scenario': 'Read a missing path without changing the filesystem.',
                      'gap_analysis': 'The read currently creates the directory.',
                      'migration_analysis': 'A local storage fix; no peer design is needed.',
                      'risks': 'Preserve creation for writes.',
                      'affected_files': ['src/storage.py', 'tests/test_storage.py'],
                      'implementation_plan': ['Make the read path avoid directory creation.'],
                      'validation_plan': ['pytest tests/test_storage.py; assert a missing path stays absent.'],
                      'feature_status': {task['repo']: target, **{
                          peer: {'status': 'unknown', 'reason': 'No analogous behavior inspected.',
                                 'checked_paths': [], 'evidence': []} for peer in peers}},
                  }}
        return task, packet, answer

    def route(self, answer, matrix, **options):
        return route(answer, matrix, reviewed=True, duplicate_clear=True, **options)

    def test_grounded_local_fixes_do_not_require_an_implemented_peer(self):
        for category in ('bug', 'tests', 'ux', 'reliability', 'performance'):
            for peers in ((), ('peer/one', 'peer/two', 'peer/three')):
                with self.subTest(category=category, peers=peers):
                    task, packet, answer = self.fixture(category, peers)
                    matrix = build(answer, task, packet)
                    self.assertEqual('READY_TO_IMPLEMENT', self.route(answer, matrix)[0])
                    self.assertTrue(all(item['status'] == 'unknown'
                                        for repo, item in matrix['feature_status'].items()
                                        if repo != task['repo']))

    def test_cross_project_feature_still_requires_gap_and_implemented_peers(self):
        task, packet, answer = self.fixture('feature', ('peer/one', 'peer/two', 'peer/three'))
        matrix = build(answer, task, packet)
        self.assertEqual('insufficient_gap_or_peer_evidence', self.route(answer, matrix)[2])
        status = answer['feature_matrix']['feature_status']['peer/one']
        status.update(status='implemented', checked_paths=['src/reader.py'],
                      evidence=[{'repo': 'peer/one', 'path': 'src/reader.py', 'quote': 'pass'}])
        matrix = build(answer, task, packet)
        self.assertEqual('NEEDS_MAINTAINER_CONFIRMATION', self.route(answer, matrix)[0])
        self.assertEqual('READY_TO_IMPLEMENT', self.route(answer, matrix, maintainer=True)[0])
        matrix['feature_status'][task['repo']]['status'] = 'implemented'
        self.assertEqual('RESEARCH_ONLY', self.route(answer, matrix, maintainer=True)[0])

    def test_unknown_or_documentation_only_target_does_not_gain_admission(self):
        task, packet, answer = self.fixture()
        target = answer['feature_matrix']['feature_status'][task['repo']]
        target['status'] = 'unknown'
        self.assertEqual('insufficient_target_fix_evidence', self.route(answer, build(answer, task, packet))[2])
        target.update(status='not_found', evidence=[{'repo': task['repo'], 'path': 'README.md',
                                                   'quote': 'Read files.'}])
        self.assertEqual('RESEARCH_ONLY', self.route(answer, build(answer, task, packet))[0])

    def test_pinned_target_quotes_and_inspection_scope_remain_required(self):
        task, packet, answer = self.fixture()
        invalid = copy.deepcopy(answer)
        invalid['feature_matrix']['feature_status'][task['repo']]['evidence'][0]['quote'] = 'invented'
        with self.assertRaisesRegex(ValueError, 'pinned source'):
            build(invalid, task, packet)
        original = build(answer, task, packet)
        for field, value in (('sha', 'b' * 40), ('checked_paths', []), ('evidence', [])):
            matrix = copy.deepcopy(original)
            matrix['feature_status'][task['repo']][field] = value
            with self.subTest(field=field):
                self.assertEqual('RESEARCH_ONLY', self.route(answer, matrix)[0])

    def test_concrete_scope_and_validation_are_required_even_after_review(self):
        task, packet, answer = self.fixture()
        original = build(answer, task, packet)
        for field, value in (('user_scenario', ''), ('gap_analysis', ''),
                             ('affected_files', []), ('affected_files', ['src/<unknown>.py']),
                             ('implementation_plan', []), ('validation_plan', [])):
            matrix = {**original, field: value}
            with self.subTest(field=field, value=value):
                self.assertEqual('RESEARCH_ONLY', self.route(answer, matrix)[0])

    def test_review_duplicate_policy_and_risk_gates_are_not_bypassed(self):
        task, packet, answer = self.fixture('ux')
        matrix = build(answer, task, packet)
        for gates in ({'reviewed': False, 'duplicate_clear': True},
                      {'reviewed': True, 'duplicate_clear': False}):
            self.assertEqual('review_or_duplicate_gate_pending', route(answer, matrix, **gates)[2])
        for policy in ({'manual_issue_submission': True}, {'human_pr_review_required': True}):
            self.assertEqual('PAUSED_POLICY', self.route(answer, matrix, policy=policy)[0])
        for flag in ('automated_issue', 'automated_pr', 'maintainer_required'):
            blocked = copy.deepcopy(answer)
            blocked['policy'][flag] = flag == 'maintainer_required'
            expected = 'PAUSED_POLICY' if flag == 'automated_issue' else 'NEEDS_MAINTAINER_CONFIRMATION'
            self.assertEqual(expected, self.route(blocked, matrix)[0])
        self.assertEqual('PAUSED_POLICY', self.route({**answer, 'risk': 'high'}, matrix,
                                                   maintainer=True, proactive=True)[0])
        self.assertEqual('RESEARCH_ONLY', self.route({**answer, 'category': 'security'}, matrix)[0])


if __name__ == '__main__':
    unittest.main()
