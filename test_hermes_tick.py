import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hermes_tick import main, tick_command


class ScheduledPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def configure(self, options):
        (self.root / 'config.json').write_text(json.dumps({'automation': options}), encoding='utf-8')

    def test_authorized_schedule_passes_global_flag_before_tick(self):
        self.configure({'scheduled_external_writes': True})
        self.assertEqual(['python', '-X', 'utf8', str(self.root / 'contribute.py'),
                          '--allow-external-writes', 'tick'], tick_command(self.root, 'python'))

    def test_disabled_and_legacy_schedules_do_not_publish(self):
        for options in ({}, {'enabled': True}, {'scheduled_external_writes': False}):
            with self.subTest(options=options):
                self.configure(options)
                self.assertNotIn('--allow-external-writes', tick_command(self.root, 'python'))

    def test_non_boolean_values_do_not_enable_publication(self):
        for value in ('true', 'false', 1, 0, None):
            with self.subTest(value=value):
                self.configure({'scheduled_external_writes': value})
                with self.assertRaises(ValueError):
                    tick_command(self.root, 'python')

    def test_launcher_uses_configured_checkout_and_preserves_exit_status(self):
        self.configure({'scheduled_external_writes': True})
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict('os.environ', {'OSS_CONTRIBUTOR_ROOT': str(self.root),
                                     'OSS_CONTRIBUTOR_PYTHON': 'chosen-python'}), \
                patch('hermes_tick.subprocess.run', return_value=SimpleNamespace(
                    returncode=7, stdout='saved work', stderr='diagnostic')) as child, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(7, main())
        self.assertEqual(tick_command(self.root, 'chosen-python'), child.call_args.args[0])
        self.assertEqual(self.root, child.call_args.kwargs['cwd'])
        self.assertIn('Scheduled publication: enabled', output.getvalue())
        self.assertIn('saved work', output.getvalue())
        self.assertIn('diagnostic', errors.getvalue())

    def test_success_delivers_bounded_maintenance_summary_without_health_noise(self):
        self.configure({'scheduled_external_writes': True})
        output = io.StringIO()
        summaries = ['PR maintenance gaps: ' + str(n) + 'x' * 2000 for n in range(8)]
        with patch.dict('os.environ', {'OSS_CONTRIBUTOR_ROOT': str(self.root)}), \
                patch('hermes_tick.subprocess.run', return_value=SimpleNamespace(
                    returncode=0, stdout='', stderr='repository owner/repo: ACTIVE\n' + '\n'.join(summaries))), \
                contextlib.redirect_stdout(output):
            self.assertEqual(0, main())
        lines = output.getvalue().splitlines()[1:]
        self.assertEqual(5, len(lines))
        self.assertTrue(all(len(line) <= 1000 for line in lines))
        self.assertNotIn('repository owner/repo', output.getvalue())


if __name__ == '__main__':
    unittest.main()
