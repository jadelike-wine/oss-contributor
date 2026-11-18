"""Unit tests for issue/PR template parsing and title sanitization (stdlib only)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from autonomous import Autonomous


TDESIGN_YML = '''name: 反馈 Bug
title: "[组件名称] 描述问题的标题"
labels: [bug]
body:
  - type: input
    attributes:
      label: tdesign-react 版本
    validations:
      required: true
  - type: textarea
    attributes:
      label: 重现步骤
  - type: textarea
    attributes:
      label: 期望结果
'''

ONLOOK_MD = '''---
name: Bug report
title: "[bug] the title of bug report"
---
#### Describe the bug
<!-- A clear description -->
'''

CHATUI_MD = '''---
name: Bug Report (chat-ui)
title: ""
---
## Bug description
<!-- desc -->
## Steps to reproduce
## Screenshots
## Context
### Logs
### Specs
### Config
## Notes
'''

CARBON_YML = '''name: Bug report
title: '[Bug]: '
body:
  - type: dropdown
    attributes:
      label: Package
  - type: checkboxes
    attributes:
      label: Is there an existing issue?
      options:
        - label: I searched
          required: true
  - type: textarea
    attributes:
      label: Description
'''


def engine_with(work):
    e = Autonomous.__new__(Autonomous)
    e.p = MagicMock()
    e.p.config = {}
    e.p.task.return_value = {'workspace': str(work), 'repo': 'o/r'}
    return e


def write_template(work, name, content):
    d = work / '.github' / 'ISSUE_TEMPLATE'
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(content, encoding='utf-8')


class TestIssueTemplate(unittest.TestCase):
    def test_tdesign_placeholder_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            write_template(work, 'bug-report.zh-CN.yml', TDESIGN_YML)
            e = engine_with(work)
            t = e.issue_template(e.p.task('x'))
            self.assertTrue(t['title_placeholder'])
            self.assertEqual(t['title_prefix'], '')
            self.assertIn('### tdesign-react 版本', t['required_headings'])
            self.assertIn('### 重现步骤', t['required_headings'])
            # any [Tag] prefix satisfies
            self.assertTrue(e.issue_title_compliant(e.p.task('x'), '[Badge] ref lost'))
            self.assertFalse(e.issue_title_compliant(e.p.task('x'), 'Badge ref lost'))

    def test_onlook_literal_bracket(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            write_template(work, 'bug_report.md', ONLOOK_MD)
            e = engine_with(work)
            t = e.issue_template(e.p.task('x'))
            self.assertFalse(t['title_placeholder'])
            self.assertEqual(t['title_prefix'], '[bug]')
            self.assertIn('#### Describe the bug', t['required_headings'])
            # HTML comment is NOT a required field
            self.assertNotIn('#### A clear description', t['required_headings'])

    def test_chatui_no_prefix_nested_headings(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            write_template(work, 'bug-report--chat-ui-.md', CHATUI_MD)
            e = engine_with(work)
            t = e.issue_template(e.p.task('x'))
            self.assertEqual(t['title_prefix'], '')
            self.assertFalse(t['title_placeholder'])
            self.assertIn('## Context', t['required_headings'])
            self.assertIn('### Logs', t['required_headings'])
            self.assertIn('### Config', t['required_headings'])
            # free title fine
            self.assertTrue(e.issue_title_compliant(e.p.task('x'), 'anything'))

    def test_carbon_literal_colon_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            write_template(work, 'BUG_REPORT.yaml', CARBON_YML)
            e = engine_with(work)
            t = e.issue_template(e.p.task('x'))
            self.assertFalse(t['title_placeholder'])
            self.assertEqual(t['title_prefix'], '[Bug]:')
            self.assertIn('### Is there an existing issue?', t['required_headings'])
            self.assertTrue(e.issue_title_compliant(e.p.task('x'), '[Bug]: broken thing'))
            self.assertFalse(e.issue_title_compliant(e.p.task('x'), 'broken thing'))

    def test_sanitize_strips_quote_and_adds_placeholder(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            write_template(work, 'bug-report.zh-CN.yml', TDESIGN_YML)
            e = engine_with(work)
            task = e.p.task('x')
            title = e.sanitize_title(task, '"Badge drops ref"', 'issue', {'plan': {'issue_title_prefix': 'Badge'}})
            self.assertEqual(title, '[Badge] Badge drops ref')
            # wrong quote-only cleanup keeps lone apostrophe
            title2 = e.sanitize_title(task, "'Tis odd title", 'issue', {'plan': {}})
            self.assertTrue(title2.startswith('['))

    def test_sanitize_replaces_wrong_bracket(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            write_template(work, 'BUG_REPORT.yaml', CARBON_YML)
            e = engine_with(work)
            task = e.p.task('x')
            title = e.sanitize_title(task, '[FEAT] broken import path', 'issue', {'plan': {}})
            self.assertEqual(title, '[Bug]: broken import path')

    def test_conventional_pr_title(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            (work / 'docs').mkdir(parents=True)
            (work / 'docs' / 'pull_request_template.md').write_text(
                'Please use conventional commit titles like feat(scope): change', encoding='utf-8')
            e = engine_with(work)
            task = e.p.task('x')
            rule, prefix = e._pr_title_rule(task)
            self.assertEqual(rule, 'conventional')
            # already-conventional title is unchanged
            self.assertEqual(e.sanitize_title(task, 'fix(button): nav bug', 'pr'), 'fix(button): nav bug')
            # non-conventional gets a bare fix: prefix, not 'fix(scope):'
            self.assertEqual(e.sanitize_title(task, 'nav bug', 'pr'), 'fix: nav bug')


if __name__ == '__main__':
    unittest.main()
