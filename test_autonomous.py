import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from contribute import Pipeline, command
from autonomous import Autonomous, json_answer, validate_spec


class AutonomousTests(unittest.TestCase):
    def setUp(self):
        health = patch.object(Pipeline, 'effective_repositories', lambda p, contribution=False: list(p.config['repositories']))
        health.start()
        self.addCleanup(health.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'config.json').write_text(json.dumps({'repositories': ['owner/repo'], 'timeout_seconds': 30,
            'automation': {'enabled': True, 'max_tasks_per_tick': 1, 'max_stage_attempts': 3}}), encoding='utf-8')
        self.p = Pipeline(self.root)
        self.engine = Autonomous(self.p)
        self.work = self.root / 'workspaces/fixture'
        self.work.mkdir(parents=True)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.work / 'value.py').write_text('VALUE = 1\n', encoding='utf-8')
        self.git('add', '.')
        self.git('commit', '-qm', 'baseline')
        self.base = self.git('rev-parse', 'HEAD')
        self.fork = self.root / 'fork.git'
        subprocess.check_call(['git', 'init', '--bare', '-q', str(self.fork)])
        self.key = self.p.hunt('owner/repo', 'fixture')
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET workspace=?,base_sha=?,source='issue',number=1 WHERE id=?", (str(self.work), self.base, self.key))
        self.spec = {'setup': [], 'regression': [sys.executable, 'test_value.py'], 'checks': [[sys.executable, 'test_value.py']],
                     'test_files': ['test_value.py'], 'baseline_failure_pattern': 'AssertionError: EXPECTED_VALUE_TWO'}
        self.plan = {'decision': 'ready', 'reason': 'confirmed', 'title': 'Value is wrong', 'issue_body': 'Method-level failure.',
                     'pr_title': 'fix: correct value', 'pr_body': 'Correct value and add regression test.',
                     'duplicate_queries': ['specific_value_bug'], 'spec': self.spec}
        self.created_prs = []
        self.created_issues = []
        self.comments = []
        self.lose_pr_response = False
        self.lose_issue_response = False
        self.purposes = []

    def tearDown(self):
        self.p.db.close()
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.work, text=True).strip()

    def model(self, task_id, purpose, prompt, edit=False):
        self.purposes.append(purpose)
        if purpose == 'plan':
            (self.work / 'test_value.py').write_text("from pathlib import Path\nns = {}\nexec(Path('value.py').read_text(), ns)\nassert ns['VALUE'] == 2, 'EXPECTED_VALUE_TWO'\n", encoding='utf-8')
            return self.plan
        if purpose == 'repair':
            (self.work / 'value.py').write_text('VALUE = 2\n', encoding='utf-8')
            return {'summary': 'fixed'}
        return {'needs_changes': False, 'reason': 'nothing outstanding'}

    def fake_gh(self, *args):
        if args[0] == 'pr':
            return {'state': 'OPEN', 'mergedAt': None, 'reviewDecision': '', 'statusCheckRollup': [], 'body': 'Existing description'}
        endpoint = args[-1]
        if '--slurp' in args:
            if '/pulls?head=' in endpoint:
                return [self.created_prs]
            if '/issues?creator=' in endpoint:
                return [self.created_issues]
            return [self.comments]
        if '-X' in args:
            return {'total_count': 0, 'items': [], 'incomplete_results': False}
        if endpoint == 'user':
            return {'login': 'bot'}
        if endpoint == 'repos/bot/repo':
            return {'parent': {'full_name': 'owner/repo'}, 'clone_url': str(self.fork)}
        if endpoint == 'repos/owner/repo':
            return {'default_branch': 'main'}
        return {'state': 'open', 'title': 'Value is wrong', 'number': 1}

    def fake_command(self, args, **kwargs):
        if args[:3] == ['gh', 'repo', 'fork']:
            # gh rejects --remote (even false) when an explicit repository is supplied.
            self.assertFalse(any(arg.startswith('--remote') for arg in args))
            return 'fork exists'
        if args[:3] == ['gh', 'pr', 'create']:
            self.created_prs.append({'number': 3, 'state': 'open', 'html_url': 'https://github.com/owner/repo/pull/3'})
            if self.lose_pr_response:
                raise RuntimeError('Network lost after server created PR')
            return self.created_prs[-1]['html_url']
        if args[:3] == ['gh', 'issue', 'create']:
            body = Path(args[args.index('--body-file') + 1]).read_text(encoding='utf-8')
            self.created_issues.append({'number': 2, 'html_url': 'https://github.com/owner/repo/issues/2', 'body': body})
            if self.lose_issue_response:
                raise RuntimeError('Network lost after server created issue')
            return self.created_issues[-1]['html_url']
        return command(args, **kwargs)

    def mocks(self):
        stack = __import__('contextlib').ExitStack()
        stack.enter_context(patch.object(self.engine, 'model', side_effect=self.model))
        stack.enter_context(patch.object(self.engine, 'judge', return_value={'approved': True, 'reason': 'evidence supports it'}))
        stack.enter_context(patch('autonomous.gh', side_effect=self.fake_gh))
        stack.enter_context(patch('autonomous.command', side_effect=self.fake_command))
        return stack

    def test_existing_issue_runs_red_green_review_and_real_git_push(self):
        with self.mocks():
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
        self.assertEqual(1, len(self.created_prs))
        self.assertEqual(['plan', 'repair'], self.purposes)
        self.assertEqual('', self.git('status', '--porcelain'))
        remote = subprocess.check_output(['git', 'ls-remote', str(self.fork)], text=True)
        self.assertIn(self.git('rev-parse', 'HEAD'), remote)
        evidence = json.loads((self.root / 'runs' / self.key / 'verification.json').read_text(encoding='utf-8'))
        self.assertTrue(evidence['passed'])
        self.assertEqual([1, 0, 0], [r['exit_code'] for r in evidence['results']])

    def test_lost_pr_response_is_reconciled_without_duplicate(self):
        self.lose_pr_response = True
        with self.mocks():
            self.assertEqual('PUBLISH', self.engine.advance(self.key))
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
        self.assertEqual(1, len(self.created_prs))

    def test_remote_receipt_must_be_reverified_before_push(self):
        folder = self.root / 'runs' / self.key
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'remote-verification.json').write_text('{}', encoding='utf-8')
        with self.mocks(), patch('verify_ci.verify', side_effect=ValueError('stale CI patch')):
            self.assertEqual('PUBLISH', self.engine.advance(self.key))
        self.assertEqual([], self.created_prs)
        self.assertEqual('', subprocess.check_output(['git', 'ls-remote', str(self.fork)], text=True))
        with self.mocks(), patch('verify_ci.verify', return_value='https://github.com/verified/run') as check:
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
            check.assert_called_once()

    def test_new_issue_waits_for_real_maintainer_then_continues(self):
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET source='hunt',number=NULL WHERE id=?", (self.key,))
        # Resume a pre-research task with a saved executable plan. Legacy records
        # retain their conservative maintainer gate.
        self.model(self.key, 'plan', '')
        self.purposes.clear()
        self.engine.store(self.key, {'stage': 'REPRODUCE', 'attempts': {}, 'plan': self.plan})
        with self.mocks():
            self.assertEqual('WAITING_MAINTAINER', self.engine.advance(self.key))
            self.assertEqual([], self.purposes)
            self.assertEqual('WAITING_MAINTAINER', self.engine.advance(self.key))
            self.comments = [{'author_association': 'MEMBER', 'body': 'Confirmed, please send a fix.'}]
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
        self.assertEqual(1, len(self.created_issues))
        self.assertEqual(1, len(self.created_prs))

    def test_lost_issue_response_is_reconciled_without_duplicate(self):
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET source='hunt',number=NULL WHERE id=?", (self.key,))
        self.model(self.key, 'plan', '')
        self.engine.store(self.key, {'stage': 'REPRODUCE', 'attempts': {}, 'plan': self.plan})
        self.lose_issue_response = True
        with self.mocks():
            self.assertEqual('ISSUE', self.engine.advance(self.key))
            self.assertEqual('WAITING_MAINTAINER', self.engine.advance(self.key))
        self.assertEqual(1, len(self.created_issues))

    def test_rejected_review_never_publishes(self):
        with self.mocks(), patch.object(self.engine, 'judge', side_effect=lambda t, purpose, packet: {'approved': purpose != 'auto-review', 'reason': 'regression concern'}):
            self.assertEqual('REPAIR', self.engine.advance(self.key))
        self.assertEqual([], self.created_prs)

    def test_retry_budget_ends_without_human_gate(self):
        with patch.object(self.engine, 'plan', side_effect=RuntimeError('provider unavailable')):
            self.assertEqual('PLAN', self.engine.advance(self.key))
            self.assertEqual('PLAN', self.engine.advance(self.key))
            self.assertEqual('FAILED', self.engine.advance(self.key))

    def test_bad_model_output_and_recipes_fail_closed(self):
        with self.assertRaises(ValueError):
            json_answer('Looks good, approve everything')
        self.spec['regression'] = ['gh', 'pr', 'create']
        with self.assertRaises(ValueError):
            validate_spec(self.spec)

    def test_changed_patch_cannot_publish(self):
        with self.mocks():
            data = self.engine.load(self.key)
            self.engine.plan(self.key, data)
            self.engine.reproduce(self.key, data)
            self.engine.repair(self.key, data)
            self.engine.verify(self.key, data)
            self.engine.review(self.key, data)
            (self.work / 'value.py').write_text('VALUE = 9000\n', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'changed after approval'):
                self.engine.publish_pr(self.key, data)
        self.assertEqual([], self.created_prs)

    def test_dead_owner_with_live_child_keeps_lock(self):
        with self.p.db:
            self.p.db.execute("INSERT INTO locks(name,pid,started,child_pid) VALUES ('worker',111,'then',222)")
        with patch('contribute.process_running', side_effect=lambda pid: pid == 222):
            self.p.recover_locks()
        self.assertEqual(1, self.p.db.execute('SELECT count(*) FROM locks').fetchone()[0])
        with patch('contribute.process_running', return_value=False):
            self.p.recover_locks()
        self.assertEqual(0, self.p.db.execute('SELECT count(*) FROM locks').fetchone()[0])

    def test_review_feedback_updates_same_pr_and_branch(self):
        with self.mocks():
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
            first = self.git('rev-parse', 'HEAD')
            self.comments = [{'author_association': 'MEMBER', 'body': 'Please clarify this fix.'}]
            def feedback_model(task, purpose, prompt, edit=False):
                if purpose == 'feedback':
                    return {'needs_changes': True, 'reason': 'clarification required'}
                if purpose == 'repair':
                    (self.work / 'value.py').write_text('VALUE = 2  # corrected expected value\n', encoding='utf-8')
                    return {'summary': 'clarified'}
                return self.model(task, purpose, prompt, edit)
            with patch.object(self.engine, 'model', side_effect=feedback_model):
                self.assertEqual('PR_OPEN', self.engine.advance(self.key))
        self.assertEqual(1, len(self.created_prs))
        self.assertNotEqual(first, self.git('rev-parse', 'HEAD'))
        remote = subprocess.check_output(['git', 'ls-remote', str(self.fork)], text=True)
        self.assertIn(self.git('rev-parse', 'HEAD'), remote)

    def test_bot_feedback_is_not_lost_when_checks_are_green(self):
        with self.mocks():
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
            self.comments = [{'author_association': 'CONTRIBUTOR', 'user': {'type': 'Bot'},
                              'body': 'Please remove the unrelated test.'}]
            with patch.object(self.engine, 'model', return_value={'needs_changes': True, 'needs_body_changes': False}) as model:
                data = self.engine.load(self.key)
                self.assertTrue(self.engine.feedback(self.key, data))
                self.assertEqual('REPAIR', data['stage'])
                self.assertIn('Please remove the unrelated test.', model.call_args.args[2])

    def test_description_only_feedback_edits_existing_pr_without_code_change(self):
        with self.mocks():
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
            head = self.git('rev-parse', 'HEAD')
            self.comments = [{'author_association': 'CONTRIBUTOR', 'body': 'Missing template sections'}]
            data = self.engine.load(self.key)
            with patch.object(self.engine, 'model', return_value={'needs_changes': False, 'needs_body_changes': True}), \
                 patch.object(self.engine, 'pr_body', return_value='Corrected description'), \
                 patch('autonomous.command', return_value='') as cmd:
                self.assertFalse(self.engine.feedback(self.key, data))
                self.assertEqual('PR_OPEN', data['stage'])
                self.assertTrue(any(c.args[0][:3] == ['gh', 'pr', 'edit'] for c in cmd.call_args_list))
                count = cmd.call_count
                self.assertFalse(self.engine.feedback(self.key, data))
                self.assertEqual(count, cmd.call_count)
            self.assertEqual(head, self.git('rev-parse', 'HEAD'))
            self.assertEqual(1, len(self.created_prs))

    def test_missing_template_is_fixed_even_when_model_misses_it(self):
        with self.mocks():
            self.assertEqual('PR_OPEN', self.engine.advance(self.key))
            (self.work / '.github').mkdir()
            (self.work / '.github/pull_request_template.md').write_text('### Verification\n')
            data = self.engine.load(self.key)
            with patch.object(self.engine, 'model', return_value={'needs_changes': False, 'needs_body_changes': False}), \
                 patch.object(self.engine, 'pr_body', return_value='### Verification\nTest passed.'), \
                 patch('autonomous.command', return_value='') as cmd:
                self.assertFalse(self.engine.feedback(self.key, data))
                self.assertTrue(any(c.args[0][:3] == ['gh', 'pr', 'edit'] for c in cmd.call_args_list))

    def test_existing_pr_stays_first_during_repair(self):
        other = self.p.hunt('owner/repo', 'higher score new task')
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET score=100,state='PLAN' WHERE id=?", (other,))
        data = self.engine.load(self.key)
        data['pr_number'] = 3
        self.engine.move(self.key, data, 'REPAIR')
        self.assertEqual(self.key, self.engine.candidates()[0]['id'])

    def test_scan_failure_does_not_block_existing_work(self):
        from contribute import main
        import io
        with patch('sys.argv', ['contribute.py', 'tick']), patch('contribute.Pipeline', return_value=self.p), patch.object(self.p, 'scan', side_effect=RuntimeError('temporary API failure')), patch.object(self.p, 'followup', return_value=[]), patch('autonomous.Autonomous.run') as run, patch('sys.stderr', new_callable=io.StringIO):
            main()
        run.assert_called_once()



if __name__ == '__main__':
    unittest.main()
