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

    def test_environment_failure_is_not_reproduction(self):
        key, path, spec = self.verification_fixture()
        spec["setup"] = [[sys.executable, "-c", "raise SystemExit(2)"]]
        path.write_text(json.dumps(spec), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Environment setup failed"):
            self.p.verify(key, path)
        result = self.root / "runs" / key / "verification.json"
        self.assertFalse(json.loads(result.read_text(encoding="utf-8"))["passed"])
        self.assertEqual("VERIFICATION_FAILED", self.p.task(key)["state"])

    def test_followup_only_reports_changes(self):
        pr = {"number": 1, "title": "fix", "state": "OPEN", "url": "https://github.com/owner/repo/pull/1", "updatedAt": "one"}
        with patch("contribute.gh", return_value=[pr]):
            self.assertEqual(1, len(self.p.followup()))
            self.assertEqual([], self.p.followup())
            pr["state"] = "MERGED"
            self.assertEqual("MERGED", self.p.followup()[0]["state"])
            self.assertEqual([], self.p.followup())

if __name__ == "__main__":
    unittest.main()
