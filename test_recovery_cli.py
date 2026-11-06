import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from contribute import Pipeline, dispatch


class RecoveryCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'config.json').write_text(json.dumps({'repositories': ['owner/repo'], 'scan_limit': 100}))
        self.p = Pipeline(self.root)

    def tearDown(self):
        self.p.db.close()
        self.temp.cleanup()

    def test_targeted_refresh_uses_research_gate_and_deduplicates_numbers(self):
        with patch.object(self.p, 'require_research') as gate, \
                patch.object(self.p, 'effective_repositories', side_effect=AssertionError('Unrelated contribution checks')), \
                patch.object(self.p, 'scan_repository', return_value=(1, 1, 0)) as scan, \
                contextlib.redirect_stdout(io.StringIO()):
            dispatch(self.p, SimpleNamespace(action='rescan-issues', repo='owner/repo', numbers=[12, 12]))
        gate.assert_called_once_with({'repo': 'owner/repo'})
        scan.assert_called_once_with('owner/repo', issue_numbers=[12])
        self.assertEqual(0, self.p.db.execute('SELECT count(*) FROM locks').fetchone()[0])

    def test_targeted_refresh_rejects_unbounded_request_before_network(self):
        with patch.object(self.p, 'scan_repository') as scan:
            for numbers in ([0], list(range(1, 12))):
                with self.assertRaises(ValueError):
                    dispatch(self.p, SimpleNamespace(action='rescan-issues', repo='owner/repo', numbers=numbers))
            scan.assert_not_called()

    def test_registration_preview_does_not_apply_or_probe_all_repositories(self):
        args = SimpleNamespace(action='register-pr', repo='owner/repo', number=42, workspace='work',
                               baseline='a' * 40, expected_head='b' * 40, expected_base='c' * 40,
                               task=None, issue=12, plan=None, apply=False)
        with patch('pr_maintenance.register_existing_pr', return_value={'applied': False}) as register, \
                patch.object(self.p, 'effective_repositories', side_effect=AssertionError('Unrelated contribution checks')), \
                patch.object(self.p, 'report') as report, contextlib.redirect_stdout(io.StringIO()):
            dispatch(self.p, args)
        self.assertFalse(register.call_args.kwargs['apply'])
        report.assert_not_called()
        self.assertEqual(0, self.p.db.execute('SELECT count(*) FROM locks').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
