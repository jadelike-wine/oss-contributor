import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from repository_catalog import CANDIDATE_GROUPS, CANDIDATES, inspect, research_pool, verify_catalog
from repository_policy import contributing_guide
from telemetry import utc
from feature_matrix import file_type


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'repositories': ['owner/target'], 'repository_checks': {'retired': {
            'old/retired': {'reason': 'Explicit retirement', 'evidence': 'https://github.com/old/retired'}}}}

    def test_committed_catalog_has_thirty_verified_distinct_research_repositories(self):
        catalog = json.loads(Path(__file__).with_name('repository-catalog.json').read_text(encoding='utf-8'))
        config = json.loads(Path(__file__).with_name('config.json').read_text(encoding='utf-8'))
        allowlisted = set(config['repositories'])
        valid = [r for r in catalog['repositories'] if r['research_allowed']]
        self.assertGreaterEqual(len({r['canonical'].lower() for r in valid}), 30)
        for record in valid:
            self.assertRegex(record['sha'], r'^[a-f0-9]{40}$')
            self.assertFalse(record['archived'])
            self.assertFalse(record['disabled'])
            self.assertEqual(record['automatic_contribution'], record['repo'] in allowlisted)
            if record['automatic_contribution']:
                self.assertEqual(record['policy']['pr_submission'], 'allowlisted')
            self.assertTrue({'source', 'test', 'documentation'}.issubset({f['file_type'] for f in record['files'].values()}))
            for path, evidence in record['files'].items():
                self.assertEqual(evidence['file_type'], file_type(path))
            self.assertTrue(record['checked_at'])
            self.assertTrue(record['default_branch'])

    def test_retired_priority_is_case_insensitive_and_never_calls_network(self):
        with patch('repository_catalog.gh') as remote:
            record = inspect('OLD/RETIRED', self.config)
        self.assertEqual(record['status'], 'retired')
        remote.assert_not_called()

    def test_expanded_default_refresh_batches_and_preserves_saved_entries(self):
        retired = {'repo': 'saved/retired', 'status': 'retired', 'reason': 'prior retirement',
                   'research_allowed': False}
        custom = {'repo': 'Custom/Project', 'status': 'paused', 'reason': 'retry', 'research_allowed': False}
        (self.root / 'repository-catalog.json').write_text(json.dumps({'repositories': [custom, retired]}))
        checked = []
        def fake_inspect(repo, config):
            checked.append(repo)
            return {'repo': repo, 'status': 'research_only', 'reason': 'fixture', 'research_allowed': True}
        with patch('repository_catalog.inspect', side_effect=fake_inspect), patch('builtins.print'):
            counts = verify_catalog(self.root, self.config)
        records = json.loads((self.root / 'repository-catalog.json').read_text())['repositories']
        expected = {r.lower() for r in [*CANDIDATES, 'Custom/Project', 'saved/retired', 'old/retired']}
        self.assertGreater(len(checked), 80)
        self.assertEqual({r['repo'].lower() for r in records}, expected)
        self.assertEqual(len(records), len(expected))
        self.assertEqual(records[0]['repo'], 'Custom/Project')
        self.assertEqual(records[1], retired)
        self.assertNotIn('saved/retired', checked)
        self.assertEqual(counts['retired'], 1)

    def test_explicit_refresh_is_case_insensitive_and_does_not_expand_scope(self):
        retired = {'repo': 'Saved/Retired', 'status': 'retired', 'reason': 'prior retirement',
                   'research_allowed': False}
        (self.root / 'repository-catalog.json').write_text(json.dumps({'repositories': [retired]}))
        with patch('repository_catalog.inspect', return_value={
                'repo': 'Owner/Project', 'status': 'paused', 'reason': 'HTTP 503', 'research_allowed': False}) as remote, \
                patch('builtins.print'):
            verify_catalog(self.root, self.config, ['Owner/Project', 'owner/project', 'SAVED/RETIRED'])
        remote.assert_called_once_with('Owner/Project', self.config)
        records = json.loads((self.root / 'repository-catalog.json').read_text())['repositories']
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], retired)
        with patch('repository_catalog.inspect') as remote:
            verify_catalog(self.root, self.config, [])
        remote.assert_not_called()

    def test_requested_expansion_contains_one_hundred_distinct_candidates(self):
        self.assertEqual([len(group) for group in CANDIDATE_GROUPS.values()], [20, 35, 20, 25])
        requested = [r.lower() for group in CANDIDATE_GROUPS.values() for r in group]
        self.assertEqual(len(set(requested)), 100)
        self.assertTrue(set(requested).issubset({r.lower() for r in CANDIDATES}))

    def test_committed_catalog_contains_every_requested_repository_without_duplicates(self):
        catalog = json.loads(Path(__file__).with_name('repository-catalog.json').read_text(encoding='utf-8'))
        names = [r['repo'].lower() for r in catalog['repositories']]
        requested = {r.lower() for group in CANDIDATE_GROUPS.values() for r in group}
        self.assertFalse(requested - set(names))
        self.assertEqual(len(names), len(set(names)))

    def test_archived_or_missing_repository_cannot_be_activated(self):
        meta = {'full_name': 'owner/archived', 'default_branch': 'main', 'pushed_at': utc(),
                'archived': True, 'disabled': False, 'private': False}
        with patch('repository_catalog.inspect_repository', return_value=(meta, None, None, None, 'Repository archived')):
            result = inspect('owner/archived', self.config)
        self.assertEqual(result['status'], 'paused')
        self.assertFalse(result['research_allowed'])
        with patch('repository_catalog.inspect_repository', side_effect=RuntimeError('HTTP 404')):
            missing = inspect('owner/missing', self.config)
        self.assertIn('404', missing['reason'])
        self.assertFalse(missing['research_allowed'])

    def test_research_permission_is_separate_and_stale_catalog_is_not_live_authority(self):
        records = [{'repo': 'peer/one', 'checked_at': utc(), 'status': 'research_only', 'research_allowed': True},
                   {'repo': 'old/retired', 'checked_at': utc(), 'status': 'active', 'research_allowed': True},
                   {'repo': 'peer/stale', 'checked_at': '2020-01-01T00:00:00+00:00', 'status': 'research_only', 'research_allowed': True}]
        (self.root / 'repository-catalog.json').write_text(json.dumps({'repositories': records}))
        p = SimpleNamespace(root=self.root, config=self.config)
        self.assertEqual(research_pool(p), ['peer/one'])
        self.assertEqual(self.config['repositories'], ['owner/target'])
        self.config['peer_repositories'] = ['unadmitted/project']
        self.assertEqual(research_pool(p), [])
        self.assertEqual(research_pool(p, respect_peer_filter=False), ['peer/one'])

    def test_batch_failure_is_isolated_and_previously_retired_record_preserved(self):
        retired = {'repo': 'saved/retired', 'status': 'retired', 'reason': 'prior retirement', 'research_allowed': False}
        (self.root / 'repository-catalog.json').write_text(json.dumps({'repositories': [retired]}))
        meta = {'full_name': 'owner/good', 'default_branch': 'main', 'pushed_at': utc(),
                'archived': False, 'disabled': False, 'private': False, 'license': {'spdx_id': 'MIT'}}
        def health(repo, *args):
            if repo == 'owner/bad':
                raise RuntimeError('HTTP 503')
            return meta, 'readme', 'contributing', None, None
        def request(*args, **kwargs):
            if '/commits/' in args[1]:
                return {'sha': 'a' * 40}
            return {'tree': [{'type': 'blob', 'path': p, 'size': 10} for p in ['app.py', 'test_app.py', 'README.md']]}
        with patch('repository_catalog.inspect_repository', side_effect=health), \
                patch('repository_catalog.gh', side_effect=request), patch('repository_catalog.command', return_value='content'):
            counts = verify_catalog(self.root, self.config, ['owner/bad', 'owner/good', 'saved/retired'])
        self.assertEqual(counts, {'retired': 1, 'paused': 1, 'research_only': 1})

    def test_shared_organization_contributing_guide_uses_official_profile(self):
        guide = {'url': 'https://api.github.com/repos/org/project/contents/CONTRIBUTING.md',
                 'html_url': 'https://github.com/org/.github/blob/main/CONTRIBUTING.md'}
        def raw(args):
            if 'repos/org/project/contents/' in args[2]:
                raise RuntimeError('HTTP 404')
            return 'Discuss new features before implementation.'
        with patch('repository_catalog.command'):
            text, endpoint = contributing_guide('org/project', lambda *a: {'files': {'contributing': guide}}, raw)
        self.assertIn('Discuss', text)
        self.assertIn('repos/org/.github/contents/', endpoint)
        guide['html_url'] = 'https://github.com/attacker/.github/blob/main/CONTRIBUTING.md'
        with self.assertRaises(RuntimeError):
            contributing_guide('org/project', lambda *a: {'files': {'contributing': guide}}, raw)


if __name__ == '__main__':
    unittest.main()
