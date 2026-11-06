import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from failures import record_setup_failure, recover_setup_change, retry_ready
from recipe_runtime import SetupError, setup_context, validate_setup
import test_conversion_runtime as runtime_fixtures


class SetupRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.node = self.work / 'node.exe'
        self.node.write_bytes(b'node runtime fixture')
        self.yarn = self.work / 'yarn.js'
        self.yarn.write_text('package manager fixture', encoding='utf8')
        self.spec = {'setup': [[str(self.node), str(self.yarn), 'install']]}

    def test_carbon_missing_yarn_entry_fails_before_launch(self):
        self.spec['setup'][0][1] = str(self.work / 'node_modules/yarn/bin/yarn.js')
        with self.assertRaisesRegex(SetupError, 'Missing executable or runtime entry') as caught:
            validate_setup(self.spec, self.work)
        data = {'stage': 'REPRODUCE', 'attempts': {'REPRODUCE': 1}, 'plan': {'spec': self.spec}}
        self.assertTrue(record_setup_failure(data, 'REPRODUCE', caught.exception, {}))
        self.assertEqual('PLAN', data['stage'])
        self.assertEqual({'REPRODUCE': 1}, data['attempts'])
        self.assertEqual('setup_recipe_replan', data['stop_reason'])

    def test_carbon_foreground_storybook_is_not_an_install_step(self):
        self.spec['setup'].append([str(self.node), str(self.yarn), '--cwd', 'packages/react', 'storybook', '-p', '3000'])
        with self.assertRaisesRegex(SetupError, 'Foreground service'):
            validate_setup(self.spec, self.work)

    def test_repository_tool_can_be_created_by_install_but_must_exist_before_its_step(self):
        generated = self.work / 'node_modules/tool/build.js'
        self.spec['setup'].append([str(self.node), str(generated)])
        validate_setup(self.spec, self.work)
        validate_setup(self.spec, self.work, step=0)
        with self.assertRaisesRegex(SetupError, 'Missing executable'):
            validate_setup(self.spec, self.work, step=1)
        generated.parent.mkdir(parents=True)
        generated.write_text('tool installed', encoding='utf8')
        validate_setup(self.spec, self.work, step=1)

    def test_server_hidden_behind_package_script_is_rejected_but_build_is_finite(self):
        (self.work / 'package.json').write_text(json.dumps({'scripts': {'docs': 'storybook dev -p 3000', 'build-docs': 'storybook build'}}), encoding='utf8')
        self.spec['setup'][0][-1:] = ['run', 'docs']
        with self.assertRaisesRegex(SetupError, 'Foreground service'):
            validate_setup(self.spec, self.work)
        self.spec['setup'][0][-1] = 'build-docs'
        self.assertIn('fingerprint', validate_setup(self.spec, self.work))

    def test_corepack_wrappers_do_not_hide_dev_servers(self):
        (self.work / 'package.json').write_text(json.dumps({'scripts': {'dev': 'vite'}}), encoding='utf8')
        corepack = self.work / 'corepack.js'
        corepack.write_text('fixture', encoding='utf8')
        for argv in ([str(self.node), str(corepack), 'pnpm', 'run', 'dev'], ['corepack', 'pnpm', 'run', 'dev']):
            with self.subTest(argv=argv), self.assertRaisesRegex(SetupError, 'Foreground service'):
                validate_setup({'setup': [argv]}, self.work)

    def test_package_names_and_echo_arguments_are_not_server_commands(self):
        from recipe_runtime import _server_command
        for command in (['npm', 'install', 'vite'], ['yarn', 'add', 'vite'],
                        ['pnpm', 'install', 'storybook'], ['echo', 'vite'],
                        "echo 'vite; storybook dev'", 'npm install vite && echo storybook'):
            with self.subTest(command=command):
                self.assertFalse(_server_command(command))
        for command in (['pnpm', 'exec', 'vite'], ['yarn', 'storybook', 'dev'], 'cross-env NODE_ENV=test vite'):
            with self.subTest(command=command):
                self.assertTrue(_server_command(command))
        validate_setup({'setup': [[str(self.node), str(self.yarn), 'add', 'vite']]}, self.work)

    def test_subpackage_manifests_change_setup_identity_for_all_directory_flags(self):
        child = self.work / 'packages/react'
        child.mkdir(parents=True)
        for flag in ('--cwd', '-C', '--dir', '--prefix', '--cwd=', '--dir=', '--prefix='):
            args = [flag + 'packages/react'] if flag.endswith('=') else [flag, 'packages/react']
            spec = {'setup': [[str(self.node), str(self.yarn), *args, 'install']]}
            package = child / 'package.json'
            package.write_text('{"packageManager":"yarn@1.22.0"}', encoding='utf8')
            before = setup_context(spec, self.work)
            package.write_text('{"packageManager":"yarn@1.22.22"}', encoding='utf8')
            with self.subTest(flag=flag):
                self.assertNotEqual(before['fingerprint'], setup_context(spec, self.work)['fingerprint'])
                data = {'stage': 'PLAN', 'plan': {'spec': spec}, 'setup_failure': {'count': 3, 'exhausted': True, 'context': before}}
                self.assertEqual('replan', recover_setup_change(data, self.work, {}))

    def test_venv_and_built_entrypoints_are_checked_at_their_own_step(self):
        python = self.work / '.venv/Scripts/python.exe'
        spec = {'setup': [[sys.executable, '-m', 'venv', '.venv'], [str(python), '-m', 'pip', 'install', 'pytest']]}
        validate_setup(spec, self.work)
        with self.assertRaisesRegex(SetupError, 'Missing executable'):
            validate_setup(spec, self.work, step=1)
        python.parent.mkdir(parents=True)
        python.write_bytes(b'created virtualenv executable')
        validate_setup(spec, self.work, step=1)

    def test_package_script_recursion_checks_target_workspace(self):
        site = self.work / 'site'
        site.mkdir()
        (self.work / 'package.json').write_text(json.dumps({'scripts': {'docs': 'pnpm -C site run dev'}}), encoding='utf8')
        (site / 'package.json').write_text(json.dumps({'scripts': {'dev': 'storybook dev'}}), encoding='utf8')
        with self.assertRaisesRegex(SetupError, 'Foreground service'):
            validate_setup({'setup': [[str(self.node), str(self.yarn), 'run', 'docs']]}, self.work)

    def test_timeout_only_reopens_after_changed_inputs_and_at_most_once(self):
        data = {'stage': 'REPRODUCE', 'attempts': {'PLAN': 2}, 'plan': {'spec': self.spec},
                'last_error': 'install timed out', 'infrastructure_failures': {'REPRODUCE':
                    {'category': 'timeout', 'count': 3, 'exhausted': True}}}
        self.assertEqual('baseline_recorded', recover_setup_change(data, self.work, {}))
        self.assertFalse(retry_ready(data, 'REPRODUCE'))
        self.assertIsNone(recover_setup_change(data, self.work, {}))
        self.node.touch()
        self.assertIsNone(recover_setup_change(data, self.work, {}), 'mtime alone is not repair evidence')
        self.yarn.write_text('repaired package manager fixture', encoding='utf8')
        self.assertEqual('replan', recover_setup_change(data, self.work, {}))
        self.assertEqual('PLAN', data['stage'])
        self.assertEqual({'PLAN': 2}, data['attempts'])
        self.assertEqual('install timed out', data['setup_recoveries'][0]['error'])
        error = SetupError('timeout again', setup_context(self.spec, self.work), 'timeout')
        for _ in range(3):
            record_setup_failure(data, 'REPRODUCE', error, {})
        self.yarn.write_text('another change', encoding='utf8')
        self.assertIsNone(recover_setup_change(data, self.work, {}))
        self.assertFalse(retry_ready(data, 'PLAN'))

    def test_no_recovery_from_test_changes_or_still_missing_tool(self):
        context = setup_context(self.spec, self.work)
        data = {'stage': 'PLAN', 'plan': {'spec': self.spec}, 'setup_failure': {'count': 3, 'exhausted': True, 'context': context}}
        (self.work / 'regression.test.js').write_text('new test', encoding='utf8')
        self.assertIsNone(recover_setup_change(data, self.work, {}))
        self.yarn.unlink()
        self.assertIsNone(recover_setup_change(data, self.work, {}))
        (self.work / 'pnpm-lock.yaml').write_text('lockfileVersion: 9.0', encoding='utf8')
        self.assertIsNone(recover_setup_change(data, self.work, {}))


class SetupSchedulerTests(unittest.TestCase):
    setUp = runtime_fixtures.RuntimeConversionTests.setUp
    tearDown = runtime_fixtures.RuntimeConversionTests.tearDown
    issue = runtime_fixtures.RuntimeConversionTests.issue

    def test_reproduction_preflight_prevents_launching_a_missing_manager(self):
        key = self.issue(1, 'REPRODUCE')
        node = self.root / 'node.exe'
        node.write_bytes(b'fixture, never execute')
        spec = {'setup': [[str(node), str(self.root / 'missing-yarn.js'), 'install']],
                'regression': [sys.executable, 'test_bug.py'], 'checks': [[sys.executable, 'test_bug.py']],
                'test_files': ['test_bug.py'], 'baseline_failure_pattern': 'REGRESSION_ASSERTION'}
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET workspace=? WHERE id=?', (str(self.root), key))
        self.engine.store(key, {'stage': 'REPRODUCE', 'attempts': {}, 'plan': {'spec': spec}})
        with patch('autonomous.execute') as execute:
            self.engine.advance(key)
        execute.assert_not_called()
        self.assertEqual('setup_error', self.engine.load(key)['failure_category'])

    def test_setup_timeout_preserves_diagnostics_and_replans_without_quality_retry(self):
        key = self.issue(1, 'REPRODUCE')
        self.p.config['timeout_seconds'] = 10
        spec = {'setup': [[sys.executable, '-c', 'pass']], 'regression': [sys.executable, 'test_bug.py'],
                'checks': [[sys.executable, 'test_bug.py']], 'test_files': ['test_bug.py'],
                'baseline_failure_pattern': 'REGRESSION_ASSERTION'}
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET workspace=? WHERE id=?', (str(self.root), key))
        self.engine.store(key, {'stage': 'REPRODUCE', 'attempts': {}, 'plan': {'spec': spec}})
        error = subprocess.TimeoutExpired(spec['setup'][0], 10, output=b'waiting for dependency download')
        with patch('autonomous.changed_files', return_value=[]), patch('autonomous.execute', side_effect=error):
            self.engine.advance(key)
        data = self.engine.load(key)
        self.assertEqual('PLAN', data['stage'])
        self.assertEqual({}, data['attempts'])
        self.assertEqual('timeout', data['setup_failure_history'][0]['domain'])
        self.assertIn('dependency download', data['last_error'])

    def test_setup_errors_replan_without_spending_patch_attempts_then_stop(self):
        key = self.issue(1, 'REPRODUCE')
        data = self.engine.load(key)
        data['attempts'] = {'REPRODUCE': 2}
        self.engine.store(key, data)
        error = SetupError('Missing Yarn entry', {'fingerprint': 'bad'})
        with patch.object(self.engine, 'reproduce', side_effect=error), patch.object(self.engine, 'plan', side_effect=error) as plan:
            self.assertEqual('PLAN', self.engine.advance(key))
            self.engine.advance(key)
            self.engine.advance(key)
            self.engine.advance(key)
        data = self.engine.load(key)
        self.assertEqual({'REPRODUCE': 2}, data['attempts'])
        self.assertEqual(3, len(data['setup_failure_history']))
        self.assertEqual(2, plan.call_count)
        self.assertTrue(data['setup_failure']['exhausted'])


if __name__ == '__main__':
    unittest.main()
