import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contribute import ContributionPaused, Pipeline, now, rank, save


class FeatureScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        save(self.root / 'config.json', {'repositories': ['owner/repo'], 'scan_limit': 100})
        self.p = Pipeline(self.root)
        self.issue = {'number': 12, 'title': 'Add a bounded option', 'body': 'Requested behavior',
                      'labels': [{'name': 'enhancement'}], 'assignees': [],
                      'url': 'https://github.com/owner/repo/issues/12'}
        self.prs = []
        self.key = 'owner--repo--12'

    def tearDown(self):
        self.p.db.close()
        self.temp.cleanup()

    def github(self, *args):
        if args == ('api', 'user'):
            return {'login': 'contributor'}
        if args[0] == 'api':
            return {'archived': False, 'full_name': 'owner/repo', 'stargazers_count': 1,
                    'default_branch': 'main', 'pushed_at': now()}
        return copy.deepcopy([self.issue] if args[0] == 'issue' else self.prs)

    def scan(self):
        with patch('contribute.gh', side_effect=self.github), \
             patch('contribute.contributing_guide', return_value=('Rules', 'CONTRIBUTING.md')), self.p.db:
            self.p.scan_repository('owner/repo')
        return self.p.task(self.key)

    def test_rank_default_holds_feature_types_but_not_component_names(self):
        for label in ('feature', 'enhancement', 'type: feature', 'kind/enhancement'):
            issue = {**self.issue, 'labels': [{'name': label}]}
            self.assertIn('feature needs maintainer approval', rank(issue)[1])
            self.assertNotIn('feature needs maintainer approval', rank(issue, allow_features=True)[1])
        component = {**self.issue, 'labels': [{'name': 'feature:st.App'}]}
        self.assertEqual([], rank(component)[1])
        self.assertIn('feature needs maintainer approval', rank(self.issue, allow_features='true')[1])

    def test_scan_opt_in_only_removes_feature_hold_and_keeps_scores(self):
        held = self.scan()
        self.assertEqual('HOLD', held['state'])
        self.p.config['feature_requests'] = {'enabled': True, 'candidates_per_tick': 2}
        admitted = self.scan()
        self.assertEqual('DISCOVERED', admitted['state'])
        self.assertEqual([], json.loads(admitted['holds']))
        self.assertEqual(held['score'], admitted['score'])
        self.issue['assignees'] = [{'login': 'another-contributor'}]
        self.issue['labels'].extend([{'name': 'security'}, {'name': 'need-information'}])
        still_held = self.scan()
        self.assertEqual('HOLD', still_held['state'])
        self.assertEqual({'assigned', 'security', 'need-information'}, set(json.loads(still_held['holds'])))

    def test_scan_missing_or_non_boolean_enable_stays_conservative(self):
        for value in (False, 'true', 1):
            self.p.config['feature_requests'] = {'enabled': value}
            self.assertEqual('HOLD', self.scan()['state'])
        self.issue['labels'] = [{'name': 'feature:st.App'}]
        self.assertEqual('DISCOVERED', self.scan()['state'])

    def test_rescan_preserves_active_stage_workspace_baseline_and_automation_history(self):
        self.scan()
        history = json.dumps({'stage': 'REPAIR', 'attempts': {'REPAIR': 2}, 'reason': 'retained evidence'})
        with self.p.db:
            self.p.db.execute('CREATE TABLE automation(task TEXT PRIMARY KEY,data TEXT NOT NULL)')
            self.p.db.execute('INSERT INTO automation VALUES (?,?)', (self.key, history))
            self.p.db.execute("UPDATE tasks SET state='REPAIR',workspace='existing-work',base_sha=? WHERE id=?",
                              ('a' * 40, self.key))
        self.p.config['feature_requests'] = {'enabled': True}
        task = self.scan()
        self.assertEqual('REPAIR', task['state'])
        self.assertEqual('existing-work', task['workspace'])
        self.assertEqual('a' * 40, task['base_sha'])
        self.assertEqual(history, self.p.db.execute('SELECT data FROM automation WHERE task=?', (self.key,)).fetchone()[0])

    def test_own_competing_pr_exception_only_removes_legacy_feature_hold(self):
        self.p.config['contribution_strategy'] = {'prefer_own_pr_on_competition': True}
        self.issue.update(author={'login': 'contributor'}, assignees=[{'login': 'somebody'}])
        self.issue['labels'].append({'name': 'security'})
        self.prs = [{'number': 99, 'url': 'https://github.com/owner/repo/pull/99',
                     'body': 'Fixes #12', 'closingIssuesReferences': [{'number': 12}]}]
        task = self.scan()
        self.assertEqual('HOLD', task['state'])
        self.assertEqual({'assigned', 'security'}, set(json.loads(task['holds'])))

    def test_manual_pr_gate_allows_local_work_but_blocks_configured_publication(self):
        task = self.scan()
        self.p.config['repository_checks'] = {'policies': {'owner/repo': {'manual_pr_submission': True}}}
        self.p._repository_evidence['owner/repo'] = ({}, 'AI-assisted development is welcome.')
        with patch.object(self.p, 'effective_repositories', return_value=['owner/repo']), \
             patch('contribute.inspect_repository', return_value=({}, '', 'AI-assisted development is welcome.', '', None)):
            self.p.require_contribution(task)
            with self.assertRaisesRegex(ContributionPaused, 'personal PR submission'):
                self.p.require_contribution(task, publishing=True)

    def test_publication_rechecks_live_autonomous_ban_before_allowing_a_pr(self):
        task = self.scan()
        self.p._repository_evidence['owner/repo'] = ({}, 'Earlier cached rules')
        rules = 'Do not submit issues, PRs, or reviews from fully autonomous AI agents.'
        with patch.object(self.p, 'effective_repositories', return_value=['owner/repo']), \
             patch('contribute.inspect_repository', return_value=({}, '', rules, '', None)) as fresh:
            self.p.require_contribution(task)
            with self.assertRaisesRegex(ContributionPaused, 'personal PR submission'):
                self.p.require_contribution(task, publishing=True)
        fresh.assert_called_once()

if __name__ == '__main__':
    unittest.main()
