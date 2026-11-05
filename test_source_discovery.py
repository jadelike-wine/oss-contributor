import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from autonomous import Autonomous
from contribute import Pipeline, now, save
from research import collect
from source_discovery import collect_scope, load_packet, pinned_scope, register_scopes, scope_options
from telemetry import BudgetExhausted, TaskBudgetExhausted


class SourceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        save(self.root / 'config.json', {'repositories': ['owner/repo'],
             'automation': {'enabled': True, 'max_stage_attempts': 3}})
        self.p = Pipeline(self.root)
        self.engine = Autonomous(self.p)
        self.records = []
        self.base = self.add_repo('owner/repo')

    def tearDown(self):
        self.p.db.close()
        self.temp.cleanup()

    def git(self, base, *args):
        return subprocess.check_output(['git', *args], cwd=base, text=True).strip()

    def commit(self, base):
        self.git(base, 'add', '.')
        self.git(base, 'commit', '-qm', 'fixture')
        sha = self.git(base, 'rev-parse', 'HEAD')
        self.git(base, 'update-ref', 'refs/remotes/origin/main', sha)
        return sha

    def add_repo(self, repo):
        base = self.root / 'workspaces' / repo.replace('/', '--')
        base.mkdir(parents=True)
        self.git(base, 'init', '-q')
        self.git(base, 'config', 'user.name', 'Fixture')
        self.git(base, 'config', 'user.email', 'fixture@example.invalid')
        self.git(base, 'remote', 'add', 'origin', 'https://github.com/' + repo + '.git')
        for name, content in {
            'storage.py': 'VALUE = 1\n',
            'tests/test_storage.py': 'from storage import VALUE\nassert VALUE == 1\n',
            'CONTRIBUTING.md': 'Small bug fixes welcome. Include a regression test.\n',
            'tests/AGENTS.md': 'Run the focused tests.\n',
        }.items():
            path = base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        self.commit(base)
        self.records.append({'repo': repo, 'default_branch': 'main', 'checked_at': now(),
            'research_allowed': True, 'status': 'active', 'archived': False, 'disabled': False,
            'contributing_url': 'repos/' + repo + '/contents/CONTRIBUTING.md'})
        save(self.root / 'repository-catalog.json', {'repositories': self.records})
        return base

    def terminal(self, key):
        data = self.engine.load(key)
        data['reason'] = 'No demonstrated defect in this exact scope'
        self.engine.move(key, data, 'SKIPPED')
        return self.p.task(key), self.engine.load(key)

    def test_same_named_tests_from_other_packages_or_fixtures_are_not_related(self):
        paths = ['infra/app.ts', 'packages/core/test/app.test.ts', 'packages/tui/test/fixture/app.ts',
                 'packages/ai/src/effect.ts', 'packages/ai/test/lib/effect.ts',
                 'deploy/kubernetes/smoke.mjs', 'deploy/kubernetes/smoke.test.mjs',
                 'packages/core/src/followup/smoke.test.ts',
                 'packages/core/src/storage.py', 'packages/core/tests/test_storage.py']
        self.assertEqual([
            ['deploy/kubernetes/smoke.mjs', 'deploy/kubernetes/smoke.test.mjs'],
            ['packages/core/src/storage.py', 'packages/core/tests/test_storage.py']], scope_options(paths))

    def test_real_pinned_source_and_applicable_rules_ignore_dirty_untracked_files(self):
        sha = self.git(self.base, 'rev-parse', 'HEAD')
        (self.base / 'storage.py').write_text('mutable content\n')
        (self.base / 'other.py').write_text('untracked\n')
        with patch('source_discovery.command', wraps=__import__('contribute').command) as commands:
            packet = collect_scope(self.p, self.records[0])
        self.assertEqual(sha, packet['target']['sha'])
        self.assertEqual('VALUE = 1\n', packet['target']['files']['storage.py'])
        self.assertEqual({'CONTRIBUTING.md', 'tests/AGENTS.md'}, set(packet['policy']))
        self.assertEqual([], packet['peers'])
        self.assertEqual([], packet['recent_issues_and_prs'])
        self.assertNotIn('other.py', packet['target']['files'])
        self.assertIn('not a live', packet['discovery_manifest']['freshness'])
        for call in commands.call_args_list:
            self.assertFalse({'fetch', 'clone'} & set(call.args[0]))
            self.assertIn('protocol.allow=never', call.args[0])
            self.assertEqual('1', call.kwargs['env']['GIT_NO_LAZY_FETCH'])

    def test_registration_no_workspace_reuses_unchanged_scope_across_new_commit(self):
        key, = register_scopes(self.engine, 2)
        original_task, original_data = self.terminal(key)
        original_packet = (self.root / 'runs' / key / 'source-discovery.json').read_bytes()
        self.assertIsNone(original_task['workspace'])
        (self.base / 'README.md').write_text('Unrelated documentation revision\n')
        self.commit(self.base)
        self.assertEqual([], register_scopes(self.engine, 2))
        self.assertEqual(original_task, self.p.task(key))
        self.assertEqual(original_data, self.engine.load(key))
        self.assertEqual(original_packet, (self.root / 'runs' / key / 'source-discovery.json').read_bytes())
        self.assertEqual(1, self.p.db.execute('SELECT count(*) FROM tasks').fetchone()[0])
        self.assertEqual(original_task['base_sha'], pinned_scope(self.p, original_task))

    def test_changed_source_or_policy_gets_new_scope_preserving_history(self):
        keys = []
        for name, content in [(None, None), ('storage.py', 'VALUE = 2\n'),
                              ('CONTRIBUTING.md', 'Small fixes welcome; run all focused tests.\n')]:
            if name:
                (self.base / name).write_text(content)
                self.commit(self.base)
            key, = register_scopes(self.engine, 1)
            self.terminal(key)
            keys.append(key)
        self.assertEqual(3, len(set(keys)))
        self.assertEqual(['SKIPPED'] * 3, [self.p.task(key)['state'] for key in keys])

    def test_persistent_module_cursor_and_per_repo_turn_with_existing_pending(self):
        (self.base / 'reader.py').write_text('READ = True\n')
        (self.base / 'tests/test_reader.py').write_text('from reader import READ\nassert READ\n')
        self.commit(self.base)
        self.add_repo('other/repo')
        keys = register_scopes(self.engine, 2)
        self.assertEqual({'owner/repo', 'other/repo'}, {self.p.task(k)['repo'] for k in keys})
        self.assertEqual(set(keys), set(register_scopes(self.engine, 2)))
        owner = next(k for k in keys if self.p.task(k)['repo'] == 'owner/repo')
        self.assertIn('reader.py', load_packet(self.p, self.p.task(owner))['target']['files'])
        self.terminal(owner)
        next_keys = register_scopes(self.engine, 2)
        next_owner = next(k for k in next_keys if self.p.task(k)['repo'] == 'owner/repo')
        self.assertIn('storage.py', load_packet(self.p, self.p.task(next_owner))['target']['files'])

    def test_unknown_nested_policy_yields_module_turn_without_registering_it(self):
        files = {'bad/reader.py': 'READ = True\n', 'bad/test_reader.py': 'assert True\n',
                 'bad/AGENTS.md': '[Policy](https://elsewhere.invalid/rules)\n'}
        for name, value in files.items():
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        self.commit(self.base)
        self.assertEqual([], register_scopes(self.engine, 1))
        self.assertEqual(0, self.p.db.execute('SELECT count(*) FROM tasks').fetchone()[0])
        self.assertEqual('bad/reader.py', self.p.db.execute(
            "SELECT last_repo FROM scheduler WHERE name='source-discovery:owner/repo'").fetchone()[0])
        key, = register_scopes(self.engine, 1)
        self.assertIn('storage.py', load_packet(self.p, self.p.task(key))['target']['files'])

    def test_registration_inspects_at_most_eight_repos_and_persists_fair_cursor(self):
        records = [{**self.records[0], 'repo': 'owner/repo' + str(i)} for i in range(10)]
        save(self.root / 'repository-catalog.json', {'repositories': records})
        with patch('source_discovery.collect_scope', side_effect=ValueError('Missing cached evidence')) as collect_one:
            self.assertEqual([], register_scopes(self.engine, 2))
            self.assertEqual(8, collect_one.call_count)
            collect_one.reset_mock()
            self.assertEqual([], register_scopes(self.engine, 2))
            self.assertEqual('owner/repo8', collect_one.call_args_list[0].args[1]['repo'])

    def test_actual_prepare_pins_registered_sha_without_fetching_latest(self):
        key, = register_scopes(self.engine, 1)
        pinned = self.p.task(key)['base_sha']
        (self.base / 'storage.py').write_text('VALUE = 42\n')
        newer = self.commit(self.base)
        self.assertNotEqual(pinned, newer)
        with patch.object(self.p, 'require_research'), \
             patch('contribute.gh', side_effect=AssertionError('No source prepare network')):
            work = Path(self.p.prepare(key, read_only=True))
        self.assertNotEqual(work, self.base)
        self.assertEqual(pinned, self.git(work, 'rev-parse', 'HEAD'))
        self.assertEqual('VALUE = 1\n', (work / 'storage.py').read_text())
        self.assertEqual(pinned, self.p.task(key)['base_sha'])
        self.assertEqual('VALUE = 42\n', (self.base / 'storage.py').read_text())

    def test_clean_all_preserves_source_pin_and_recreates_the_same_version(self):
        key, = register_scopes(self.engine, 1)
        pinned = self.p.task(key)['base_sha']
        evidence = self.root / 'runs' / key / 'source-discovery.json'
        original_evidence = evidence.read_bytes()
        with patch.object(self.p, 'require_research'), \
             patch('contribute.gh', side_effect=AssertionError('No source prepare network')):
            work = Path(self.p.prepare(key, read_only=True))
            (self.base / 'storage.py').write_text('VALUE = 42\n')
            self.commit(self.base)
            self.p.clean_workspaces(all_worktrees=True)
            self.assertFalse(work.exists())
            self.assertIsNone(self.p.task(key)['workspace'])
            self.assertEqual(pinned, self.p.task(key)['base_sha'])
            rebuilt = Path(self.p.prepare(key, read_only=True))
        self.assertEqual(pinned, self.git(rebuilt, 'rev-parse', 'HEAD'))
        self.assertEqual('VALUE = 1\n', (rebuilt / 'storage.py').read_text())
        self.assertEqual(original_evidence, evidence.read_bytes())

    def test_source_read_only_reentry_rejects_moved_head_without_overwriting_work(self):
        key, = register_scopes(self.engine, 1)
        with patch.object(self.p, 'require_research'):
            work = Path(self.p.prepare(key, read_only=True))
            untracked = work / 'untracked-local.txt'
            untracked.write_text('Preserve this local evidence.\n')
            with self.assertRaisesRegex(RuntimeError, 'local changes'):
                self.p.prepare(key, read_only=True)
            self.assertTrue(untracked.exists())
            untracked.unlink()
            (work / 'storage.py').write_text('VALUE = 42\n')
            with self.assertRaisesRegex(RuntimeError, 'local changes'):
                self.p.prepare(key, read_only=True)
            self.git(work, 'add', 'storage.py')
            self.git(work, 'commit', '-qm', 'preserved local implementation')
            changed = self.git(work, 'rev-parse', 'HEAD')
            with self.assertRaisesRegex(RuntimeError, 'registered fixed SHA'):
                self.p.prepare(key, read_only=True)
        with patch.object(self.p, 'require_contribution'):
            self.assertEqual(str(work), self.p.prepare(key, read_only=False))
        self.assertEqual(changed, self.git(work, 'rev-parse', 'HEAD'))
        self.assertEqual('VALUE = 42\n', (work / 'storage.py').read_text())

    def test_normative_links_and_backtick_rule_paths_are_collected_completely(self):
        files = {
            'CONTRIBUTING.md': 'Follow the [style guide](docs/style.md) and '
                               '[design documentation requirements](docs/requirements.md).\n\n'
                               'When creating a PR, follow `.github/pull_request_template.md`.\n',
            'docs/style.md': 'Use clear names.\n',
            'docs/requirements.md': 'Explain observable behavior and validation.\n',
            '.github/pull_request_template.md': 'Describe the regression and tests.\n',
        }
        for name, content in files.items():
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.commit(self.base)
        packet = collect_scope(self.p, self.records[0])
        for name, content in files.items():
            self.assertEqual(content, packet['policy'][name])

    def test_unread_required_rule_reference_stays_unknown(self):
        for content in ('Follow [the rules](missing.md).\n', 'Read `missing.md` before making changes.\n'):
            (self.base / 'CONTRIBUTING.md').write_text(content)
            self.commit(self.base)
            self.assertEqual([], register_scopes(self.engine, 1))
            self.assertEqual(0, self.p.db.execute('SELECT count(*) FROM tasks').fetchone()[0])
            report = json.loads((self.root / 'runs/source-discovery-summary.json').read_text())
            self.assertEqual('unknown', report['repositories'][0]['status'])
            self.assertIn('missing.md', report['repositories'][0]['reason'])

    def test_required_bug_template_is_collected_without_unrelated_feature_template(self):
        files = {
            'CONTRIBUTING.md': 'Bug reports and feature requests must use their issue templates. '
                               'Ask support questions in the [community chat](https://chat.example.invalid).\n',
            '.github/ISSUE_TEMPLATE/bug-report.yml': 'name: Bug report\nbody: []\n',
            '.github/ISSUE_TEMPLATE/config.yml': 'blank_issues_enabled: false\n',
            '.github/ISSUE_TEMPLATE/feature-request.yml': 'name: Feature proposal\nbody: []\n',
        }
        for name, content in files.items():
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.commit(self.base)
        packet = collect_scope(self.p, self.records[0])
        self.assertIn('.github/ISSUE_TEMPLATE/bug-report.yml', packet['policy'])
        self.assertIn('.github/ISSUE_TEMPLATE/config.yml', packet['policy'])
        self.assertNotIn('.github/ISSUE_TEMPLATE/feature-request.yml', packet['policy'])

    def test_incomplete_or_unverified_material_never_registers(self):
        for case in ('oversized_policy', 'external_policy', 'missing_clone', 'stale_catalog'):
            with self.subTest(case=case):
                if case == 'oversized_policy':
                    (self.base / 'CONTRIBUTING.md').write_text('x' * 60001)
                    self.commit(self.base)
                elif case == 'external_policy':
                    (self.base / 'CONTRIBUTING.md').write_text('[Policy](https://elsewhere.invalid/rules)\n')
                    self.commit(self.base)
                elif case == 'missing_clone':
                    self.records[0]['repo'] = 'absent/repo'
                    save(self.root / 'repository-catalog.json', {'repositories': self.records})
                else:
                    self.records[0]['checked_at'] = '2000-01-01T00:00:00+00:00'
                    save(self.root / 'repository-catalog.json', {'repositories': self.records})
                self.assertEqual([], register_scopes(self.engine, 2))
                self.assertEqual(0, self.p.db.execute('SELECT count(*) FROM tasks').fetchone()[0])

    def test_full_larger_policy_is_retained_but_whole_packet_still_bounded(self):
        policy = 'Small fixes welcome.\n' * 1000
        (self.base / 'CONTRIBUTING.md').write_text(policy)
        self.commit(self.base)
        packet = collect_scope(self.p, self.records[0])
        self.assertEqual(policy, packet['policy']['CONTRIBUTING.md'])
        self.p.config['research'] = {'context_characters': 1000}
        self.assertEqual([], register_scopes(self.engine, 1))
        self.assertEqual(0, self.p.db.execute('SELECT count(*) FROM tasks').fetchone()[0])

    def test_tampered_saved_content_or_inventory_cannot_supply_pinned_sha(self):
        key, = register_scopes(self.engine, 1)
        task = self.p.task(key)
        path = self.root / 'runs' / key / 'source-discovery.json'
        original = json.loads(path.read_text())
        for change in ('source', 'policy', 'paths', 'manifest'):
            packet = copy.deepcopy(original)
            if change == 'source':
                packet['target']['files']['storage.py'] = 'VALUE = 9\n'
            elif change == 'policy':
                packet['policy']['CONTRIBUTING.md'] = 'Invented permission'
            elif change == 'paths':
                packet['target']['checked_paths'].append('unread.py')
            else:
                packet['discovery_manifest']['sha'] = 'f' * 40
            save(path, packet)
            with self.assertRaises(ValueError):
                pinned_scope(self.p, task)

    def test_source_collection_uses_retained_packet_without_peer_or_network_calls(self):
        key, = register_scopes(self.engine, 1)
        with patch('research.gh', side_effect=AssertionError('network')), \
             patch('research.snapshot', side_effect=AssertionError('peer collection')), \
             patch('research.command', side_effect=AssertionError('source was already collected')):
            packet = collect(self.engine, self.p.task(key))
        self.assertTrue(packet['complete'])
        self.assertEqual([], packet['selected_peers'])
        self.assertTrue((self.root / 'runs' / key / 'research-context.json').exists())
        self.p.config['research'] = {'context_characters': 1000}
        with self.assertRaisesRegex(TaskBudgetExhausted, 'context_characters'):
            collect(self.engine, self.p.task(key))
        self.assertTrue((self.root / 'runs' / key / 'research-context.json').exists())

    def research_source(self, category):
        from test_feature_matrix import matrix_answer
        key, = register_scopes(self.engine, 1)
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET workspace=? WHERE id=?', (str(self.base), key))
        task = self.p.task(key)
        answer = json.loads(json.dumps(matrix_answer(task, [])).replace('value.py', 'storage.py'))
        answer['category'] = category
        answer['feature_matrix']['feature_status'][task['repo']]['evidence'] = copy.deepcopy(answer['evidence'])
        return key, answer

    def test_target_only_feature_is_retained_research_only_without_review_or_publication(self):
        key, answer = self.research_source('feature')
        with patch.object(self.p, 'require_research'), patch.object(self.p, 'prepare'), \
             patch.object(self.engine, 'model', return_value=answer), \
             patch.object(self.engine, 'duplicates') as duplicate, patch.object(self.engine, 'judge') as judge:
            self.engine.research(key, self.engine.load(key))
        duplicate.assert_not_called()
        judge.assert_not_called()
        self.assertEqual('SKIPPED', self.engine.load(key)['stage'])
        self.assertFalse(self.engine.load(key).get('research', {}).get('approved'))
        self.assertEqual('RESEARCH_ONLY', self.p.db.execute(
            'SELECT queue FROM research_candidates WHERE task=?', (key,)).fetchone()[0])
        self.assertTrue((self.root / 'runs' / key / 'opportunity.json').exists())

    def test_target_only_local_fix_still_requires_duplicate_and_reviewer_gates(self):
        key, answer = self.research_source('bug')
        with patch.object(self.p, 'require_research'), patch.object(self.p, 'prepare'), \
             patch.object(self.engine, 'model', return_value=answer), \
             patch.object(self.engine, 'duplicates', return_value=True) as duplicate, \
             patch.object(self.engine, 'judge', return_value={'approved': True, 'reason': 'Concrete local defect'}) as judge:
            self.engine.research(key, self.engine.load(key))
        duplicate.assert_called_once()
        judge.assert_called_once()
        data = self.engine.load(key)
        self.assertTrue(data['research']['approved'])
        self.assertEqual('PLAN', data['stage'])
        self.assertEqual(self.p.fingerprint(self.base), data['research']['fingerprint'])
        self.assertEqual('local_discovery', data['contribution_origin']['kind'])
        self.assertEqual('READY_TO_IMPLEMENT', self.p.db.execute(
            'SELECT queue FROM research_candidates WHERE task=?', (key,)).fetchone()[0])

    def test_local_network_cap_checkpoints_without_reclassifying_global_budget(self):
        from telemetry import ACTIVE
        key = self.p.hunt('owner/repo', 'source scope fixture')
        task = {**self.p.task(key), 'workspace': str(self.base),
                'base_sha': self.git(self.base, 'rev-parse', 'HEAD')}
        self.p.config['research'] = {'network_requests': 1}
        with patch('research.select_peers', return_value=[]), patch('research.gh', return_value=[]):
            with self.assertRaisesRegex(TaskBudgetExhausted, 'network_requests'):
                collect(self.engine, task)
        checkpoint = json.loads((self.root / 'runs' / key / 'research-context.json').read_text())
        self.assertFalse(checkpoint['complete'])
        self.assertEqual(task['base_sha'], checkpoint['target']['sha'])
        checkpoint.pop('recent_issues_and_prs')
        save(self.root / 'runs' / key / 'research-context.json', checkpoint)
        from types import SimpleNamespace
        global_budget = BudgetExhausted('real_global_limit')
        token = ACTIVE.set(SimpleNamespace(remaining=lambda: (_ for _ in ()).throw(global_budget)))
        try:
            with self.assertRaises(BudgetExhausted) as raised:
                collect(self.engine, task)
            self.assertIs(global_budget, raised.exception)
            self.assertNotIsInstance(raised.exception, TaskBudgetExhausted)
        finally:
            ACTIVE.reset(token)


if __name__ == '__main__':
    unittest.main()
