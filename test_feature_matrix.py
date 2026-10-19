import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from feature_matrix import build, persist, route, file_type
from research import collect, validate, snapshot, select_paths, search_target
from telemetry import BudgetExhausted
from test_autonomous import AutonomousTests as Fixture
from test_research import opportunity


def matrix_answer(task, peers):
    answer = opportunity(task['repo'])
    answer['category'] = 'feature'
    statuses = {task['repo']: {'status': 'not_found', 'reason': 'Only constant found in inspected file',
                               'checked_paths': ['value.py'], 'evidence': []}}
    for peer in peers:
        statuses[peer] = {'status': 'implemented', 'reason': 'Executable implementation', 'checked_paths': ['feature.py'],
                          'evidence': [{'repo': peer, 'path': 'feature.py', 'quote': 'VALUE = 2'}]}
    answer['feature_matrix'] = {'feature_key': 'value-resolution', 'feature_name': 'Resolve value', 'category': 'tools',
        'user_scenario': 'Callers resolve a value', 'gap_analysis': 'Existing constant does not resolve input',
        'migration_analysis': 'Reuse interface design, adapt target execution lifecycle', 'risks': 'Behavior compatibility',
        'affected_files': ['value.py'], 'implementation_plan': ['Add resolver and regression'],
        'validation_plan': ['python test_value.py: baseline assertion fails; patch passes'], 'feature_status': statuses}
    return answer


class MatrixTests(unittest.TestCase):
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    git = Fixture.git

    def test_session_selection_uses_baseline_content_and_preserves_runtime_coverage(self):
        files = {
            'src/minisweagent/agents/default.py': 'def save():\n    return "trajectory"\n',
            'src/minisweagent/agents/interactive.py': 'history = []\n',
            'src/minisweagent/models/utils/actions_text.py': 'actions = []\n',
            'src/minisweagent/py.typed': '',
            'tests/agents/__init__.py': '',
            'tests/agents/test_default.py': 'def test_save():\n    assert "trajectory"\n',
            'docs/recovery.md': 'Resume a session\n',
        }
        for name, body in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        self.git('add', '.')
        self.git('commit', '-qm', 'session fixture')
        sha = self.git('rev-parse', 'HEAD')
        focus = 'Compare session recovery and checkpoints without repeating completed actions'
        # An untracked or edited file must not change what baseline research sees.
        (self.work / 'src/minisweagent/agents/default.py').write_text('unrelated = True\n')
        (self.work / 'untracked.py').write_text('resume = True\n')
        matches = search_target(self.work, sha, focus, list(files))
        self.assertIn('src/minisweagent/agents/default.py', matches)
        self.assertNotIn('untracked.py', matches)
        selected = select_paths(list(files), focus, 4, matches)
        self.assertIn('src/minisweagent/agents/default.py', selected)
        self.assertIn('tests/agents/test_default.py', selected)
        self.assertIn('docs/recovery.md', selected)
        self.assertNotIn('src/minisweagent/py.typed', selected)
        self.assertNotIn('tests/agents/__init__.py', selected)

    def test_content_search_no_hits_is_not_a_command_failure(self):
        self.assertEqual([], search_target(self.work, self.base, 'nonexistentcapability', ['value.py']))

    def test_large_search_inventory_does_not_exhaust_source_context(self):
        task = self.p.task(self.key)
        matches = [f'src/session/{i:05d}/' + 'x' * 80 + '.py' for i in range(5000)]
        self.p.config['research'] = {'context_characters': 24000}
        with patch('repository_catalog.research_pool', return_value=[]), \
                patch('research.search_target', return_value=matches), patch('research.gh', return_value=[]):
            packet = collect(self.engine, task)
        search = packet['target']['content_search']
        self.assertEqual(search['matched_count'], len(matches))
        self.assertTrue(search['matched_paths_truncated'])
        self.assertLessEqual(len(search['matched_paths']), 100)
        self.assertLessEqual(len(json.dumps(packet, ensure_ascii=False)), 24000)
        self.assertIn('value.py', packet['target']['files'])
        self.assertTrue(packet['complete'])

    def test_oversized_legacy_search_checkpoint_resumes_without_rereading_sources(self):
        task = self.p.task(self.key)
        with patch('repository_catalog.research_pool', return_value=[]), patch('research.gh', return_value=[]):
            packet = collect(self.engine, task)
        original_files = copy.deepcopy(packet['target']['files'])
        matches = ['src/' + 'x' * 200 + str(i) + '.py' for i in range(5000)]
        packet['target']['content_search']['matched_paths'] = matches
        packet['target']['content_search'].pop('matched_count', None)
        packet['target']['content_search'].pop('matched_paths_truncated', None)
        checkpoint = self.engine.packet_dir(self.key) / 'research-context.json'
        checkpoint.write_text(json.dumps(packet), encoding='utf-8')
        with patch('research.command') as command, patch('research.gh') as remote:
            resumed = collect(self.engine, task)
            again = collect(self.engine, task)
        command.assert_not_called()
        remote.assert_not_called()
        self.assertEqual(resumed['target']['files'], original_files)
        self.assertEqual(resumed['target']['content_search']['matched_count'], len(matches))
        self.assertTrue(resumed['target']['content_search']['matched_paths_truncated'])
        self.assertLessEqual(len(json.dumps(resumed['target']['content_search']['matched_paths'])), 12002)
        self.assertEqual(again, resumed)
        self.assertTrue(resumed['complete'])

    def test_legacy_status_nesting_keeps_all_evidence_checks(self):
        task, packet, answer = self.fixture()
        answer['feature_status'] = answer['feature_matrix'].pop('feature_status')
        self.assertIn(task['repo'], build(answer, task, packet)['feature_status'])
        answer['feature_matrix']['feature_status'] = {}
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            build(answer, task, packet)

    def test_correction_reports_bad_quotes_and_unread_scope_together(self):
        from research import correction_feedback
        task, packet, answer = self.fixture()
        answer['feature_matrix']['feature_status'][task['repo']]['checked_paths'] = ['unread.py']
        answer['evidence'][0]['quote'] = 'invented excerpt'
        feedback = correction_feedback(answer, packet, ValueError('first error'))
        self.assertIn('unread.py', feedback)
        self.assertIn('invented excerpt', feedback)

    def test_policy_files_do_not_displace_reserved_source_and_test_reads(self):
        (self.work / 'AGENTS.md').write_text('Rules\n')
        (self.work / 'CONTRIBUTING.md').write_text('Contribution rules\n')
        (self.work / 'test_value.py').write_text('assert True\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'policy coverage')
        task = {**self.p.task(self.key), 'base_sha': self.git('rev-parse', 'HEAD')}
        self.p.config['research'] = {'target_files': 4}
        with patch('repository_catalog.research_pool', return_value=[]), patch('research.gh', return_value=[]):
            packet = collect(self.engine, task)
        self.assertEqual({'AGENTS.md', 'CONTRIBUTING.md', 'value.py', 'test_value.py'}, set(packet['target']['files']))

    def fixture(self):
        task = self.p.task(self.key)
        peers = ['peer/one', 'peer/two', 'peer/three']
        packet = {'schema_version': 2, 'policy': {},
                  'target': {'repo': task['repo'], 'sha': self.base, 'files': {'value.py': 'VALUE = 1'}},
                  'peers': [{'repo': r, 'sha': str(i) * 40, 'files': {'feature.py': 'VALUE = 2'}} for i, r in enumerate(peers, 1)]}
        return task, packet, matrix_answer(task, peers)

    def test_multiple_distinct_peers_and_pinned_evidence_survive_export(self):
        task, packet, answer = self.fixture()
        matrix = build(answer, task, packet)
        queue, duplicate = persist(self.engine, task, answer, matrix, reviewed=True, duplicate_clear=True)
        self.assertEqual(queue, 'NEEDS_MAINTAINER_CONFIRMATION')
        self.assertFalse(duplicate)
        saved = json.loads((self.engine.packet_dir(self.key) / 'feature-matrix.json').read_text())
        self.assertEqual(len(saved['peer_repositories']), 3)
        self.assertEqual(saved['target_sha'], self.base)
        self.assertEqual([e['sha'] for e in saved['evidence']], ['1' * 40, '2' * 40, '3' * 40])
        self.assertTrue(all(e['feature_id'] == matrix['feature_id'] for e in saved['evidence']))

    def test_four_statuses_serialize_and_partial_requires_missing_behavior(self):
        task, packet, answer = self.fixture()
        statuses = answer['feature_matrix']['feature_status']
        statuses['peer/two'].update(status='partial', missing='No cancellation')
        statuses['peer/three'].update(status='unknown', evidence=[], checked_paths=[])
        matrix = json.loads(json.dumps(build(answer, task, packet)))
        self.assertEqual({s['status'] for s in matrix['feature_status'].values()}, {'implemented', 'partial', 'not_found', 'unknown'})
        del statuses['peer/two']['missing']
        with self.assertRaisesRegex(ValueError, 'missing behavior'):
            build(answer, task, packet)

    def test_failed_peer_cannot_be_not_found_or_implemented(self):
        task, packet, answer = self.fixture()
        packet['peers'][0]['error'] = 'TimeoutExpired'
        for state in ('not_found', 'implemented'):
            answer['feature_matrix']['feature_status']['peer/one']['status'] = state
            with self.assertRaisesRegex(ValueError, 'unknown'):
                build(answer, task, packet)

    def test_readme_only_does_not_prove_implementation_or_absence(self):
        task, packet, answer = self.fixture()
        packet['peers'][0]['files'] = {'README.md': 'Supported feature'}
        status = answer['feature_matrix']['feature_status']['peer/one']
        status.update(checked_paths=['README.md'], evidence=[{'repo': 'peer/one', 'path': 'README.md', 'quote': 'Supported feature'}])
        for state in ('implemented', 'not_found'):
            status['status'] = state
            with self.assertRaises(ValueError):
                build(answer, task, packet)

    def test_target_peer_mix_fabricated_quote_and_wrong_sha_rejected(self):
        task, packet, answer = self.fixture()
        for changes in ({'repo': task['repo']}, {'quote': 'invented'}, {'sha': 'wrong'}):
            invalid = copy.deepcopy(answer)
            invalid['feature_matrix']['feature_status']['peer/one']['evidence'][0].update(changes)
            with self.assertRaises(ValueError):
                build(invalid, task, packet)

    def test_not_found_scope_must_have_been_read(self):
        task, packet, answer = self.fixture()
        answer['feature_matrix']['feature_status'][task['repo']]['checked_paths'] = ['not-read.py']
        with self.assertRaisesRegex(ValueError, 'actually read'):
            build(answer, task, packet)

    def test_candidate_deduplication_and_idempotent_updates(self):
        task, packet, answer = self.fixture()
        matrix = build(answer, task, packet)
        persist(self.engine, task, answer, matrix)
        created = matrix['created_at']
        persist(self.engine, task, answer, build(answer, task, packet), reviewed=True, duplicate_clear=True)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM research_candidates').fetchone()[0], 1)
        self.assertEqual(self.p.db.execute('SELECT created_at FROM research_candidates').fetchone()[0], created)
        duplicate_task = {**task, 'id': 'different-task'}
        self.assertEqual(persist(self.engine, duplicate_task, answer, matrix), ('RESEARCH_ONLY', True))
        self.assertEqual(self.p.db.execute('SELECT task FROM research_candidates').fetchone()[0], task['id'])

    def test_approval_never_overrides_policy_or_missing_evidence(self):
        task, packet, answer = self.fixture()
        matrix = build(answer, task, packet)
        gates = {'reviewed': True, 'duplicate_clear': True, 'maintainer': True}
        self.assertEqual(route(answer, matrix, **gates)[0], 'READY_TO_IMPLEMENT')
        for policy in ({'human_pr_review_required': True}, {'manual_issue_submission': True}):
            self.assertEqual(route(answer, matrix, policy=policy, **gates)[0], 'PAUSED_POLICY')
        matrix['feature_status'][task['repo']]['status'] = 'unknown'
        self.assertEqual(route(answer, matrix, **gates)[0], 'RESEARCH_ONLY')
        # automated_pr=false routes through maintainer discussion, not a dead end.
        answer['policy']['automated_pr'] = False
        self.assertEqual(route(answer, matrix, **gates)[0], 'RESEARCH_ONLY')
        answer['policy']['automated_issue'] = False
        self.assertEqual(route(answer, matrix, **gates)[0], 'PAUSED_POLICY')

    def test_bug_remains_eligible_and_feature_priority_requires_gates(self):
        bug = opportunity()
        self.assertEqual(route(bug, None, reviewed=True, duplicate_clear=True)[0], 'READY_TO_IMPLEMENT')
        feature = {**bug, 'category': 'feature'}
        self.assertEqual(route(feature, None)[1], 0)
        self.assertGreater(route(feature, None, reviewed=True, duplicate_clear=True)[1], route(bug, None, reviewed=True, duplicate_clear=True)[1])

    def test_collect_limits_peers_and_recovers_completed_snapshots(self):
        task = self.p.task(self.key)
        self.p.config['research'] = {'peers_per_candidate': 3}
        reads = []
        def peer(repo, *args):
            reads.append(repo)
            return {'repo': repo, 'sha': 'a' * 40, 'files': {'feature.py': 'VALUE = 2'}}
        with patch('repository_catalog.research_pool', return_value=[task['repo'], *['peer/' + str(i) for i in range(6)]]), \
                patch('research.snapshot', side_effect=peer), patch('research.gh', return_value=[]):
            packet = collect(self.engine, task)
            again = collect(self.engine, task)
        self.assertEqual(len(reads), 3)
        self.assertEqual(len(set(reads)), 3)
        self.assertEqual(packet, again)
        self.assertEqual(packet['target']['sha'], self.base)

    def test_peer_failure_is_recorded_unknown_and_others_continue(self):
        task = self.p.task(self.key)
        def peer(repo, *args):
            if repo == 'peer/bad':
                raise RuntimeError('unreachable')
            return {'repo': repo, 'sha': 'a' * 40, 'files': {'feature.py': 'VALUE = 2'}}
        with patch('repository_catalog.research_pool', return_value=['peer/a', 'peer/bad', 'peer/c']), \
                patch('research.snapshot', side_effect=peer), patch('research.gh', return_value=[]):
            packet = collect(self.engine, task)
        failure = next(p for p in packet['peers'] if p['repo'] == 'peer/bad')
        self.assertEqual(failure['status'], 'unknown')
        self.assertEqual(len(packet['peers']), 3)

    def test_network_and_context_budget_preserve_checkpoint(self):
        self.p.config['research'] = {'network_requests': 1}
        with patch('repository_catalog.research_pool', return_value=[]), patch('research.gh', return_value=[]):
            with self.assertRaises(BudgetExhausted):
                collect(self.engine, self.p.task(self.key))
            checkpoint = self.engine.packet_dir(self.key) / 'research-context.json'
            self.assertIn('target', json.loads(checkpoint.read_text()))
            self.p.config['research']['network_requests'] = 40
            self.p.config['research']['context_characters'] = 1000
            data = json.loads(checkpoint.read_text())
            data['target']['files']['value.py'] = 'X' * 2000
            checkpoint.write_text(json.dumps(data))
            with self.assertRaises(BudgetExhausted):
                collect(self.engine, self.p.task(self.key))
            self.assertFalse(json.loads(checkpoint.read_text())['complete'])

    def test_invalid_peer_count_cannot_expand_network_work(self):
        self.p.config['research'] = {'peers_per_candidate': 99}
        with patch('research.gh') as remote:
            with self.assertRaises(ValueError):
                collect(self.engine, self.p.task(self.key))
        remote.assert_not_called()

    def test_snapshot_file_count_characters_and_sha_are_enforced(self):
        calls = []
        responses = [{'default_branch': 'main'}, {'sha': 'a' * 40},
                     {'tree': [{'type': 'blob', 'path': 'src/tool' + str(i) + '.py', 'size': 100} for i in range(20)]}]
        def raw(argv):
            calls.append(argv)
            return 'x' * 1000
        with patch('research.gh', side_effect=responses):
            source = snapshot('peer/one', 'tool', {'files_per_peer': 4, 'file_characters': 100}, raw=raw)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all('?ref=' + 'a' * 40 in c[2] for c in calls))
        self.assertTrue(all(len(text) == 100 for text in source['files'].values()))

    def test_failed_file_read_preserves_known_peer_sha(self):
        replies = [{'default_branch': 'main'}, {'sha': 'a' * 40},
                   {'tree': [{'type': 'blob', 'path': 'tool.py', 'size': 10}]}]
        with patch('research.gh', side_effect=replies), patch('research.command', side_effect=RuntimeError('HTTP 503')):
            source = snapshot('peer/one')
        self.assertEqual(source['sha'], 'a' * 40)
        self.assertEqual(source['error'], 'RuntimeError')
        self.assertEqual(source['files'], {})

    def test_documentation_and_configuration_are_not_executable_test_evidence(self):
        for path in ('specs/session.md', 'tests/README.md', 'tests/AGENTS.md'):
            self.assertEqual(file_type(path), 'documentation')
        for path in ('.env.test', '.github/workflows/test.yml', 'vibe.spec', 'ty-tests.toml'):
            self.assertEqual(file_type(path), 'configuration')
        for path in ('tests/__init__.py', 'ag2/spec.py', 'entity/tool_spec.py'):
            self.assertEqual(file_type(path), 'source')
        for path in ('tests/test_recovery.py', 'session.test.ts', 'flow_test.go'):
            self.assertEqual(file_type(path), 'test')

    def test_duplicate_peer_and_wrong_target_baseline_are_rejected(self):
        task, packet, answer = self.fixture()
        packet['peers'].append(copy.deepcopy(packet['peers'][0]))
        with self.assertRaisesRegex(ValueError, 'distinct'):
            build(answer, task, packet)
        packet['peers'].pop()
        packet['target']['sha'] = 'f' * 40
        with self.assertRaisesRegex(ValueError, 'baseline'):
            build(answer, task, packet)

    def test_mutable_branch_name_is_not_accepted_as_evidence_version(self):
        task, packet, answer = self.fixture()
        packet['peers'][0]['sha'] = 'main'
        with self.assertRaisesRegex(ValueError, 'immutable SHA'):
            build(answer, task, packet)


del Fixture
