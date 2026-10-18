import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import catalog_sweep as sweep
from contribute import Pipeline, save


class CatalogSweepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config.json').write_text(json.dumps({'repositories': []}), encoding='utf-8')
        self.p = Pipeline(self.root)
        self.addCleanup(self.p.db.close)
        self.output = self.root / 'runs/sweep'
        self.packet = {'repo': 'owner/repo', 'sha': 'a' * 40,
                       'issues': [{'number': 7, 'title': 'specific bug'}],
                       'rules': ['CONTRIBUTING.md'], 'files': {'src/a.py': 'VALUE = 1',
                       'CONTRIBUTING.md': 'Automated contributions are welcome.'}}

    def candidate(self):
        return {'decision': 'candidate', 'reason': 'Needs a focused reproduction', 'issue_number': 7,
                'policy': {'automated_pr': True, 'maintainer_required': False, 'human_review_required': False},
                'evidence': [{'path': 'src/a.py', 'quote': 'VALUE = 1'}],
                'policy_evidence': [{'path': 'CONTRIBUTING.md', 'quote': 'Automated contributions are welcome.'}]}

    def test_summary_keeps_all_32_repositories_and_missing_results_unknown(self):
        pool = [f'owner/repo{i}' for i in range(32)]
        save(self.output / 'owner--repo0-scan.json', {'repo': pool[0], 'status': 'scanned', 'issues': 10})
        result = sweep.summary(self.output, pool)
        self.assertEqual([r['repo'] for r in result], pool)
        self.assertEqual(result[0]['scan_status'], 'scanned')
        self.assertTrue(all(r['triage_status'] == 'unknown' for r in result))
        self.assertEqual(result[-1]['scan_status'], 'unknown')

    def test_specific_report_path_is_selected_alongside_tests_and_docs(self):
        paths = ['src/a.py', 'tests/test_a.py', 'docs/a.md', 'src/nested/reported.py', 'src/other.py']
        result = sweep.select_sources(paths, [{'title': 'unexpected result', 'body': '`src/nested/reported.py`'}], 4)
        self.assertIn('src/nested/reported.py', result)
        self.assertIn('tests/test_a.py', result)
        self.assertIn('docs/a.md', result)

    def test_candidate_requires_read_issue_exact_quotes_and_policy(self):
        sweep.validate(self.candidate(), self.packet)
        for mutation in ('issue', 'quote', 'policy', 'truncated', 'rules_only', 'malformed_path', 'malformed_policy'):
            candidate, packet = self.candidate(), dict(self.packet)
            if mutation == 'issue':
                candidate['issue_number'] = 999
            elif mutation == 'quote':
                candidate['evidence'][0]['quote'] = 'fabricated'
            elif mutation == 'policy':
                candidate['policy_evidence'] = []
            elif mutation == 'truncated':
                packet['rules_truncated'] = True
            elif mutation == 'rules_only':
                candidate['evidence'] = candidate['policy_evidence']
            elif mutation == 'malformed_path':
                candidate['evidence'][0]['path'] = []
            else:
                candidate['policy'] = None
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                sweep.validate(candidate, packet)

    def test_pr_inventory_bounds_nested_graphql_records_and_retains_related_pr(self):
        prs = [{'number': n, 'title': 'A PR', 'url': f'url/{n}', 'closingIssuesReferences': []} for n in range(200)]
        prs[-1]['closingIssuesReferences'] = [{'number': 7, 'body': 'X' * 300000}]
        summary = sweep.pr_summaries(prs, self.packet['issues'])
        self.assertEqual(len(summary), 30)
        self.assertEqual(summary[0]['number'], 199)
        self.assertEqual(summary[0]['closing_issue_numbers'], [7])
        self.assertLess(len(json.dumps(summary)), 20000)

    def test_model_correction_is_bounded_and_success_is_checkpointed_without_publication(self):
        engine = Mock()
        engine.hermes_research.side_effect = [json.JSONDecodeError('malformed', '{', 1), self.candidate()]
        with patch('catalog_sweep.Autonomous', return_value=engine):
            result = sweep.analyze(self.p, self.output, self.packet)
            again = sweep.analyze(self.p, self.output, self.packet)
        self.assertEqual(engine.hermes_research.call_count, 2)
        self.assertEqual(result, again)
        self.assertFalse(result['published'])
        self.assertEqual(result['execution_result'], 'not_run')
        engine.advance.assert_not_called()
        engine.publish_pr.assert_not_called()

    def test_quote_correction_retains_rejected_answer_and_identifies_offending_quote(self):
        invalid = self.candidate()
        invalid['evidence'][0]['quote'] = 'invented line'
        engine = Mock()
        engine.hermes_research.side_effect = [invalid, self.candidate()]
        with patch('catalog_sweep.Autonomous', return_value=engine):
            sweep.analyze(self.p, self.output, self.packet)
        diagnostic = json.loads((sweep.folder(self.output, 'owner/repo') / 'invalid-0.json').read_text())
        self.assertEqual(diagnostic['response'], invalid)
        correction = engine.hermes_research.call_args_list[1].args[2]
        self.assertIn('invented line', correction)
        self.assertIn('src/a.py', correction)
        self.assertIn('short literal single-line', correction)

    def test_single_attempt_respects_remaining_budget_without_implicit_correction(self):
        engine = Mock()
        engine.hermes_research.return_value = []
        with patch('catalog_sweep.Autonomous', return_value=engine), self.assertRaises(ValueError):
            sweep.analyze(self.p, self.output, self.packet, attempts=1)
        self.assertEqual(engine.hermes_research.call_count, 1)

    def test_failed_correction_is_not_saved_as_a_success(self):
        engine = Mock()
        engine.hermes_research.return_value = {'decision': 'invalid'}
        with patch('catalog_sweep.Autonomous', return_value=engine), self.assertRaises(ValueError):
            sweep.analyze(self.p, self.output, self.packet)
        self.assertEqual(engine.hermes_research.call_count, 2)
        self.assertFalse((sweep.folder(self.output, 'owner/repo') / 'triage.json').exists())

    def test_complete_collection_checkpoint_does_not_repeat_network_reads(self):
        save(sweep.folder(self.output, 'owner/repo') / 'packet.json', self.packet)
        with patch('catalog_sweep.gh') as network:
            result = sweep.collect(self.root, self.output, 'owner/repo', {})
        self.assertEqual(result, self.packet)
        network.assert_not_called()

    def test_collection_exposes_oversized_rules_and_reads_ai_policy_and_readme_fallback(self):
        repo, sha = 'owner/repo', 'a' * 40
        save(self.output / 'owner--repo-scan.json', {'status': 'scanned', 'started': '2026-01-01'})
        save(self.root / 'runs/snapshots/owner--repo/open-prs.json', [])
        entries = [('AGENTS.md', 60001), ('.github/AI_POLICY.md', 100),
                   ('README.md', 100), ('src/a.py', 100), ('tests/test_a.py', 100)]
        def network(*args, **kwargs):
            endpoint = args[1]
            if endpoint == 'repos/' + repo:
                return {'full_name': repo, 'default_branch': 'main'}
            if '/commits/' in endpoint:
                return {'sha': sha}
            if '/git/trees/' in endpoint:
                return {'tree': [{'path': p, 'size': size, 'type': 'blob'} for p, size in entries]}
            raise AssertionError(endpoint)
        with patch('catalog_sweep.gh', side_effect=network), patch('catalog_sweep.command', return_value='Read rule or source'):
            result = sweep.collect(self.root, self.output, repo, {})
        self.assertTrue(result['rules_truncated'])
        self.assertEqual(result['unread_rules'], ['AGENTS.md'])
        self.assertIn('.github/AI_POLICY.md', result['rules'])
        self.assertIn('README.md', result['rules'])
        self.assertNotIn('AGENTS.md', result['files'])

    def test_scan_failure_preserves_previous_tasks_and_reports_unknown(self):
        def fail(pipeline, repo):
            pipeline.db.execute("INSERT INTO scheduler VALUES ('partial','bad')")
            raise RuntimeError('network unavailable')
        with patch.object(Pipeline, 'scan_repository', fail):
            result = sweep.scan_repository(self.root, self.output, 'owner/repo')
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('issues', result)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM scheduler').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
