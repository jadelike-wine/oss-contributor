import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch
from contribute import Pipeline, rank, repo_name

class PipelineTests(unittest.TestCase):
    def setUp(self):
        health = patch.object(Pipeline, 'effective_repositories', lambda p, contribution=False: list(p.config['repositories']))
        health.start()
        self.addCleanup(health.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config.json").write_text(json.dumps({"repositories": ["owner/repo"], "scan_limit": 100}), encoding="utf-8")
        self.p = Pipeline(self.root)

    def tearDown(self):
        self.p.db.close()
        self.temp.cleanup()

    def test_hunt_idempotency_and_allowlist(self):
        first = self.p.hunt("owner/repo", "cancellation")
        self.assertEqual(first, self.p.hunt("owner/repo", "cancellation"))
        self.assertEqual(1, self.p.db.execute("SELECT count(*) FROM tasks").fetchone()[0])
        with self.assertRaises(ValueError):
            self.p.hunt("other/repo", "test")

    def test_repo_traversal_rejected(self):
        for value in ("../../secret", "../..", "owner/repo/extra", "owner/repo;echo hi"):
            with self.assertRaises(ValueError):
                repo_name(value)

    def test_lock_exclusion_and_release_on_failure(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with self.p.lock("worker"):
                with self.assertRaisesRegex(RuntimeError, "Lock worker exists"):
                    with self.p.lock("worker"):
                        pass
                raise RuntimeError("boom")
        self.assertEqual(0, self.p.db.execute("SELECT count(*) FROM locks").fetchone()[0])

    def test_human_and_security_tasks_held(self):
        _, holds = rank({"title": "bug", "labels": [{"name": "status/ready-for-human"}, {"name": "category/security"}], "assignees": [{"login": "someone"}]})
        self.assertEqual(3, len(holds))

    def test_scan_duplicate_pr_and_resume_state(self):
        issue = {"number": 12, "title": "Timeout bug", "body": "Steps to reproduce", "labels": [{"name": "type/bug"}], "url": "https://github.com/owner/repo/issues/12"}
        prs = [{"number": 50, "body": "Fixes #123", "url": "https://github.com/owner/repo/pull/50", "closingIssuesReferences": []}]
        def fake_gh(*args):
            if args[0] == "api":
                return {"archived": False, "full_name": "owner/repo", "stargazers_count": 20000, "default_branch": "main", "pushed_at": "today"}
            return [issue] if args[0] == "issue" else prs
        with patch("contribute.gh", side_effect=fake_gh), patch("contribute.command", return_value="Rules"):
            self.assertEqual(1, self.p.scan())
            self.assertEqual("DISCOVERED", self.p.task("owner--repo--12")["state"])
            self.assertEqual(0, self.p.scan())
            prs[0]["body"] = "Fixes #12"
            self.p.scan()
            self.assertEqual("HOLD", self.p.task("owner--repo--12")["state"])
            with self.p.db:
                self.p.db.execute("UPDATE tasks SET state='NEEDS_REPRODUCTION'")
            self.p.scan()
            self.assertEqual("NEEDS_REPRODUCTION", self.p.task("owner--repo--12")["state"])
            self.assertEqual(1, self.p.db.execute("SELECT count(*) FROM tasks").fetchone()[0])

    def verification_fixture(self):
        self.p.config["timeout_seconds"] = 30
        work = self.root / "workspaces" / "fixture"
        work.mkdir(parents=True)
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=work, text=True).strip()
        git("init", "-q")
        (work / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
        git("add", "value.py")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline")
        sha = git("rev-parse", "HEAD")
        (work / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
        (work / "test_value.py").write_text("from pathlib import Path\nnamespace = {}\nexec(Path('value.py').read_text(), namespace)\nassert namespace['VALUE'] == 2, 'REGRESSION_VALUE_MISMATCH'\n", encoding="utf-8")
        key = self.p.hunt("owner/repo", "fixture")
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET state='PATCH_READY',workspace=?,base_sha=? WHERE id=?", (str(work), sha, key))
        spec = {"setup": [], "regression": [sys.executable, "test_value.py"], "checks": [[sys.executable, "test_value.py"]], "test_files": ["test_value.py"], "baseline_failure_pattern": "AssertionError: REGRESSION_VALUE_MISMATCH"}
        path = self.root / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return key, path, spec

    def test_verification_runs_real_red_green_and_detects_stale_patch(self):
        key, path, _ = self.verification_fixture()
        result = Path(self.p.verify(key, path))
        evidence = json.loads(result.read_text(encoding="utf-8"))
        self.assertTrue(evidence["passed"])
        self.assertNotEqual(0, evidence["results"][0]["exit_code"])
        self.assertEqual(0, evidence["results"][1]["exit_code"])
        work = Path(self.p.task(key)["workspace"])
        (work / "value.py").write_text("VALUE = 3\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "exact patch"):
            self.p.review(key)

    def test_diagnostic_verify_rejects_unsafe_recipe_before_execution(self):
        key, path, spec = self.verification_fixture()
        spec["checks"] = [["git", "push", "origin", "HEAD"]]
        path.write_text(json.dumps(spec), encoding="utf-8")
        with patch("contribute.execute") as run:
            with self.assertRaisesRegex(ValueError, "publishing or shell"):
                self.p.verify(key, path)
        run.assert_not_called()
        self.assertEqual("PATCH_READY", self.p.task(key)["state"])

    def test_diagnostic_verify_rejects_generic_failure_oracle(self):
        key, path, spec = self.verification_fixture()
        spec["baseline_failure_pattern"] = ".*"
        path.write_text(json.dumps(spec), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "specific baseline assertion"):
            self.p.verify(key, path)
        self.assertEqual("PATCH_READY", self.p.task(key)["state"])

    def test_diagnostic_analyze_isolates_config_and_tracks_child(self):
        key, _, _ = self.verification_fixture()
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET source='issue',number=1 WHERE id=?", (key,))
        self.p.config.update(model="tokenking/devin/swe-2", opencode_executable="opencode")
        work = self.p.task(key)["workspace"]
        (self.root / "runs" / key).mkdir(parents=True)
        observed = {}
        def start(args, **kwargs):
            observed["env"] = kwargs["env"]
            kwargs["stdout"].write(json.dumps({"type": "text", "part": {"text": "Static report"}}) + "\n")
            process = unittest.mock.Mock(pid=424242)
            def wait(**kwargs):
                observed["child"] = self.p.db.execute("SELECT child_pid FROM locks WHERE name='worker'").fetchone()[0]
                return 0
            process.wait.side_effect = wait
            return process
        with patch.object(self.p, "prepare", return_value=work), patch("contribute.gh", return_value={'state': 'OPEN', 'title': 'Bug'}), patch("contribute.opencode_executable", return_value="opencode"), patch("contribute.subprocess.Popen", side_effect=start):
            self.p.analyze(key)
        with self.subTest("project config isolation"):
            self.assertEqual("1", observed["env"].get("OPENCODE_DISABLE_PROJECT_CONFIG"))
        with self.subTest("live child lock"):
            self.assertEqual(424242, observed["child"])
        self.assertEqual("NEEDS_REPRODUCTION", self.p.task(key)["state"])
        self.assertEqual(0, self.p.db.execute("SELECT count(*) FROM locks").fetchone()[0])

    def test_environment_failure_is_not_reproduction(self):
        key, path, spec = self.verification_fixture()
        spec["setup"] = [[sys.executable, "-c", "raise SystemExit(2)"]]
        path.write_text(json.dumps(spec), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Environment setup failed"):
            self.p.verify(key, path)
        result = self.root / "runs" / key / "verification.json"
        self.assertFalse(json.loads(result.read_text(encoding="utf-8"))["passed"])
        self.assertEqual("VERIFICATION_FAILED", self.p.task(key)["state"])

    def test_followup_detects_bot_comments_even_with_unchanged_green_checks(self):
        pr = {"number": 1, "title": "fix", "state": "OPEN", "url": "https://github.com/owner/repo/pull/1", "updatedAt": "one", "statusCheckRollup": []}
        comments = []
        with patch("contribute.gh", side_effect=lambda *args: [pr.copy()] if args[0] == "pr" else [comments.copy()]):
            self.p.followup()
            self.assertEqual([], self.p.followup())
            comments.append({"body": "Required template missing", "author_association": "CONTRIBUTOR"})
            self.assertEqual(1, len(self.p.followup()))
            self.assertEqual([], self.p.followup())
            self.assertEqual([], json.loads((self.root / 'runs/pr-updates.json').read_text()))

    def test_followup_only_reports_changes(self):
        pr = {"number": 1, "title": "fix", "state": "OPEN", "url": "https://github.com/owner/repo/pull/1", "updatedAt": "one"}
        with patch("contribute.gh", side_effect=lambda *args: [pr.copy()] if args[0] == "pr" else [[]]):
            self.assertEqual(1, len(self.p.followup()))
            self.assertEqual([], self.p.followup())
            pr["state"] = "MERGED"
            self.assertEqual("MERGED", self.p.followup()[0]["state"])
            self.assertEqual([], self.p.followup())

    def workspace_fixture(self):
        """Real base clone + two linked worktrees; one terminal task, one in-flight."""
        base = self.root / "workspaces" / "owner--repo"
        base.mkdir(parents=True)
        def git(*args, cwd=base):
            return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()
        git("init", "-q")
        (base / "code.py").write_text("x = 1\n", encoding="utf-8")
        git("add", "code.py")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
        sha = git("rev-parse", "HEAD")
        done = self.p.hunt("owner/repo", "done focus")
        live = self.p.hunt("owner/repo", "live focus")
        # Worktree directories are named after the task id, matching prepare().
        for tid in (done, live):
            git("worktree", "add", "--detach", str(self.root / "workspaces" / tid), sha)
            with self.p.db:
                self.p.db.execute("UPDATE tasks SET workspace=?,base_sha=? WHERE id=?",
                                  (str(self.root / "workspaces" / tid), sha, tid))
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET state='SKIPPED' WHERE id=?", (done,))
            self.p.db.execute("UPDATE tasks SET state='PLAN' WHERE id=?", (live,))
        # Orphaned worktree with no task row.
        git("worktree", "add", "--detach", str(self.root / "workspaces" / "owner--repo--orphan"), sha)
        return base, sha, done, live

    def test_clean_workspaces_removes_terminal_and_orphans_only(self):
        base, sha, done, live = self.workspace_fixture()
        result = self.p.clean_workspaces()
        removed = {x['name'] for x in result['removed']}
        self.assertIn(done, removed)
        self.assertIn('owner--repo--orphan', removed)
        self.assertEqual({live}, set(result['kept']))
        self.assertTrue((self.root / 'workspaces' / live).is_dir())
        self.assertFalse((self.root / 'workspaces' / done).exists())
        self.assertFalse((self.root / 'workspaces/owner--repo--orphan').exists())
        # Workspace columns cleared so prepare re-creates instead of erroring.
        task = self.p.task(done)
        self.assertIsNone(task['workspace'])
        self.assertIsNone(task['base_sha'])
        # Git registration pruned: only base + live remain.
        listed = subprocess.check_output(["git", "worktree", "list"], cwd=base, text=True)
        self.assertIn(live, listed)
        self.assertNotIn(done, listed)

    def test_clean_workspaces_all_removes_in_flight_and_dry_run_changes_nothing(self):
        base, sha, done, live = self.workspace_fixture()
        self.p.clean_workspaces(dry_run=True)
        self.assertTrue((self.root / 'workspaces' / done).is_dir())
        self.assertIsNotNone(self.p.task(done)['workspace'])
        result = self.p.clean_workspaces(all_worktrees=True)
        removed = {x['name'] for x in result['removed']}
        self.assertIn(live, removed)
        for tid in (done, live):
            self.assertFalse((self.root / 'workspaces' / tid).exists(), tid)
            self.assertIsNone(self.p.task(tid)['workspace'])
        self.assertFalse((self.root / 'workspaces/owner--repo--orphan').exists())
        # Base clone itself survives.
        self.assertTrue((base / '.git').is_dir())

    def test_followup_detects_workflow_approval_hidden_by_green_rollup(self):
        pr = {'number': 1, 'title': 'fix', 'state': 'OPEN', 'url': 'https://github.com/owner/repo/pull/1',
              'updatedAt': 'unchanged', 'headRefOid': 'exact-head',
              'statusCheckRollup': [{'name': 'standards', 'conclusion': 'SUCCESS'}]}
        workflow = {'id': 7, 'workflow_id': 9, 'head_sha': 'exact-head', 'name': 'test',
                    'status': 'completed', 'conclusion': 'action_required'}
        def github(*args):
            if args[0] == 'pr':
                return [pr.copy()]
            if '/actions/runs?' in args[-1]:
                return [{'workflow_runs': [workflow.copy()]}]
            return [[]]
        with patch('contribute.gh', side_effect=github):
            observed = self.p.followup()[0]
            self.assertIn('ci', observed)
            self.assertEqual('approval_required', observed['ci']['state'])
            self.assertEqual([], self.p.followup())
            workflow['conclusion'] = 'success'
            self.assertEqual('observed_checks_passed', self.p.followup()[0]['ci']['state'])

if __name__ == "__main__":
    unittest.main()
