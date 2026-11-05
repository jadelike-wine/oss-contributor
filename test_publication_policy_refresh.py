"""Fresh target policy must beat stale health caches without widening authority."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from contribute import ContributionPaused, Pipeline, now, save


class PublicationPolicyRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = 'owner/repo'
        save(self.root / 'config.json', {'repositories': [self.repo],
             'repository_checks': {'cache_seconds': 86400, 'policies': {}}})
        self.p = Pipeline(self.root)
        self.addCleanup(self.p.db.close)
        self.task = {'id': 'one', 'repo': self.repo, 'number': 1}
        self.meta = {'full_name': self.repo, 'pushed_at': now()}
        self.path = self.root / 'runs/snapshots/owner--repo'
        self.path.mkdir(parents=True)
        (self.path / 'CONTRIBUTING.md').write_text('Contributions welcome.', encoding='utf-8')
        self.old = {'active': True, 'contribute': True, 'checked_at': now(), 'reason': None}
        save(self.root / 'state/repositories.json', {'repositories': {self.repo: self.old}})

    def result(self, rules='Contributions welcome.', reason=None):
        return (self.meta, 'README', rules, 'repos/owner/repo/contents/docs/CONTRIBUTING.md', reason)

    def test_first_publication_read_does_not_replace_new_ban_with_cached_guide(self):
        rules = 'Do not submit PRs from fully autonomous AI agents.'
        with patch('contribute.inspect_repository', return_value=self.result(rules)):
            with self.assertRaisesRegex(ContributionPaused, 'personal PR submission'):
                self.p.require_contribution(self.task, publishing=True)
        self.assertEqual(rules, self.p._repository_evidence[self.repo][1])

    def test_current_success_recovers_stale_network_pause_for_this_target(self):
        self.p._repository_status = {self.repo: {**self.old, 'active': False, 'contribute': False,
                                              'reason': 'Health/policy check failed: network unavailable'}}
        with patch('contribute.inspect_repository', return_value=self.result()):
            self.p.require_contribution(self.task, publishing=True)
        self.assertTrue(self.p._repository_status[self.repo]['contribute'])

    def test_fresh_rules_and_real_path_are_retained_for_next_process(self):
        self.p._repository_status = {self.repo: self.old}
        rules = 'Only start work once a maintainer has added the `ready` label.'
        issue = {'state': 'open', 'labels': [{'name': 'ready'}]}
        with patch('contribute.inspect_repository', return_value=self.result(rules)), \
                patch('contribute.gh', return_value=issue):
            self.p.require_contribution(self.task, refresh=True)
        self.assertEqual(rules, (self.path / 'CONTRIBUTING.md').read_text(encoding='utf-8'))
        self.assertEqual('docs/CONTRIBUTING.md', self.p.contributing_path(self.repo))
        persisted = json.loads((self.root / 'state/repositories.json').read_text(encoding='utf-8'))
        self.assertEqual(self.p._repository_status[self.repo], persisted['repositories'][self.repo])

    def test_current_ready_issue_can_recover_previous_board_read_failure(self):
        self.p.config['repository_checks']['policies'][self.repo] = {'ready_project': 'https://github.com/orgs/owner/projects/1'}
        self.p._repository_status = {self.repo: {**self.old, 'contribute': False,
                                              'reason': 'Cannot verify required Ready board: timeout'}}
        with patch('contribute.inspect_repository', return_value=self.result()), \
                patch('contribute.ready_issue', return_value=True) as ready:
            self.p.require_contribution(self.task, publishing=True)
        ready.assert_called_once()

    def test_current_rules_do_not_relax_missing_ready_or_human_review(self):
        self.p._repository_status = {self.repo: self.old}
        rules = 'Only start work once a maintainer has added the `ready` label.'
        with patch('contribute.inspect_repository', return_value=self.result(rules)), \
                patch('contribute.gh', return_value={'state': 'open', 'labels': []}):
            with self.assertRaisesRegex(ContributionPaused, 'ready'):
                self.p.require_contribution(self.task, publishing=True)
        self.p.config['repository_checks']['policies'][self.repo] = {'human_pr_review_required': True}
        with patch('contribute.inspect_repository', return_value=self.result()):
            with self.assertRaisesRegex(ContributionPaused, 'personal code review'):
                self.p.require_contribution(self.task, publishing=True)

    def test_retired_or_unallowlisted_repository_is_never_revived(self):
        self.p.config['repository_checks']['retired'] = {self.repo: {'reason': 'Explicit retirement', 'evidence': 'saved'}}
        self.p._repository_status = {self.repo: {**self.old, 'contribute': False, 'active': False,
                                              'reason': 'Explicit retirement'}}
        with patch('contribute.inspect_repository', return_value=self.result()):
            with self.assertRaises(ContributionPaused):
                self.p.require_contribution(self.task, publishing=True)
        self.p.config['repository_checks']['retired'] = {}
        self.p.config['repositories'] = []
        with patch('contribute.inspect_repository', return_value=self.result()):
            with self.assertRaises(ContributionPaused):
                self.p.require_contribution(self.task, publishing=True)

    def test_current_archive_or_network_failure_does_not_fall_back_to_cached_success(self):
        self.p._repository_status = {self.repo: self.old}
        with patch('contribute.inspect_repository', return_value=self.result(reason='Repository is archived')):
            with self.assertRaisesRegex(ContributionPaused, 'archived'):
                self.p.require_contribution(self.task, publishing=True)
        with patch('contribute.inspect_repository', side_effect=RuntimeError('network unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'network unavailable'):
                self.p.require_contribution(self.task, publishing=True)

    def test_explicit_request_to_disable_autonomous_prs_is_preserved(self):
        rules = ('We use AI tooling constantly and have no problem with you using it too. '
                 'If you have an agent filing PRs against open issues autonomously, '
                 'please turn it off for this repository.')
        self.p._repository_status = {self.repo: self.old}
        with patch('contribute.inspect_repository', return_value=self.result(rules)):
            with self.assertRaisesRegex(ContributionPaused, 'personal PR submission'):
                self.p.require_contribution(self.task, publishing=True)


if __name__ == '__main__':
    unittest.main()
