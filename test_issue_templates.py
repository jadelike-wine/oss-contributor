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

MIMO_YML = '''name: Bug report
description: Report an issue that should be fixed
labels: ["bug"]
body:
  - type: textarea
    id: description
    attributes:
      label: Description
    validations:
      required: true
  - type: input
    id: os
    attributes:
      label: Operating System
'''

MIMO_BLOCK_YML = '''name: Bug report
labels:
  - bug
  - "help wanted"
body:
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

    def test_template_labels_inline(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            write_template(work, 'bug-report.yml', MIMO_YML)
            e = engine_with(work)
            t = e.issue_template(e.p.task('x'))
            self.assertEqual(t['labels'], ['bug'])

    def test_template_labels_block(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            write_template(work, 'bug-report.yml', MIMO_BLOCK_YML)
            e = engine_with(work)
            t = e.issue_template(e.p.task('x'))
            self.assertEqual(t['labels'], ['bug', 'help wanted'])

    def test_template_labels_absent(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            write_template(work, 'BUG_REPORT.yaml', CARBON_YML)
            e = engine_with(work)
            t = e.issue_template(e.p.task('x'))
            self.assertEqual(t['labels'], [])

    def test_template_labels_parsing_edge_cases(self):
        self.assertEqual(Autonomous._template_labels('labels: []'), [])
        self.assertEqual(Autonomous._template_labels('labels: bug'), ['bug'])
        self.assertEqual(Autonomous._template_labels('labels: [bug, "help wanted"]'), ['bug', 'help wanted'])
        self.assertEqual(Autonomous._template_labels('labels:\n  - bug\nbody:\n  - type: input'), ['bug'])
        self.assertEqual(Autonomous._template_labels('name: x\nbody:\n  - type: input'), [])

    def _issue_engine(self, work):
        e = Autonomous.__new__(Autonomous)
        e.p = MagicMock()
        e.p.config = {}
        e.p.allow_external_writes = True
        e.p.task.return_value = {'workspace': str(work), 'repo': 'o/r'}
        e.issue_body = lambda task_id, data: '### Description\n\nx\n'
        e.sanitize_title = lambda task, title, kind, data=None: title
        e.packet_dir = lambda task_id: work
        e.store = lambda task_id, data: None
        e.register_issue = lambda task_id, data, issue: issue
        return e

    def test_create_issue_applies_template_labels(self):
        import autonomous
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            write_template(work, 'bug-report.yml', MIMO_YML)
            e = self._issue_engine(work)
            calls = []

            def fake_command(args, **kw):
                calls.append(list(args))
                return 'https://github.com/o/r/issues/1' if 'create' in args else ''

            orig = autonomous.command
            autonomous.command = fake_command
            try:
                e.create_issue('t1', {'plan': {'title': 'broken thing'}})
            finally:
                autonomous.command = orig
            create = [c for c in calls if 'create' in c][0]
            edit = [c for c in calls if 'edit' in c][0]
            # Labels are applied after creation, never as part of `issue create`.
            self.assertNotIn('--label', create)
            self.assertEqual(edit[edit.index('--add-label') + 1], 'bug')

    def test_create_issue_survives_label_permission_failure(self):
        import autonomous
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            write_template(work, 'bug-report.yml', MIMO_YML)
            e = self._issue_engine(work)
            data = {'plan': {'title': 'broken thing'}}

            def fake_command(args, **kw):
                if 'create' in args:
                    return 'https://github.com/o/r/issues/2'
                raise RuntimeError('jadelike-wine does not have the correct permissions')

            orig = autonomous.command
            autonomous.command = fake_command
            try:
                issue = e.create_issue('t1', data)
            finally:
                autonomous.command = orig
            self.assertEqual(issue['number'], 2)
            self.assertIn('label_warning', data)

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


class TestEnsureOpenPr(unittest.TestCase):
    def _engine(self, gh_responses):
        e = Autonomous.__new__(Autonomous)
        e.p = MagicMock()
        e.p.task.return_value = {'workspace': '/tmp/w', 'repo': 'o/r'}
        calls = []
        def fake_gh(*args, **kw):
            calls.append(args)
            key = ' '.join(str(a) for a in args)
            for pattern, resp in gh_responses.items():
                if pattern in key:
                    return resp
            return {}
        e._gh = fake_gh
        return e, calls

    def test_skips_when_not_draft(self):
        e, calls = self._engine({'pr view': {'draft': False, 'state': 'OPEN', 'id': 'n1'}})
        # Patch module-level gh
        import autonomous
        orig = autonomous.gh
        autonomous.gh = e._gh
        try:
            e.ensure_open_pr('t1', e.p.task('x'), 5)
        finally:
            autonomous.gh = orig
        graphql = [c for c in calls if 'graphql' in ' '.join(c)]
        self.assertEqual(graphql, [])

    def test_converts_draft(self):
        e, calls = self._engine({
            'pr view': {'draft': True, 'state': 'OPEN', 'id': 'PR_node123'},
            'graphql': {'data': {'markPullRequestReadyForReview': {'pullRequest': {'isDraft': False, 'state': 'OPEN'}}}},
        })
        import autonomous
        orig = autonomous.gh
        autonomous.gh = e._gh
        try:
            e.ensure_open_pr('t1', e.p.task('x'), 5)
        finally:
            autonomous.gh = orig
        graphql = [c for c in calls if 'graphql' in ' '.join(c)]
        self.assertEqual(len(graphql), 1)
        self.assertIn('markPullRequestReadyForReview', ' '.join(graphql[0]))

    def test_fails_when_still_draft(self):
        e, calls = self._engine({
            'pr view': {'draft': True, 'state': 'OPEN', 'id': 'PR_node123'},
            'graphql': {'data': {'markPullRequestReadyForReview': {'pullRequest': {'isDraft': True, 'state': 'OPEN'}}}},
        })
        import autonomous
        orig = autonomous.gh
        autonomous.gh = e._gh
        try:
            with self.assertRaises(RuntimeError) as ctx:
                e.ensure_open_pr('t1', e.p.task('x'), 5)
            self.assertIn('remains draft', str(ctx.exception))
        finally:
            autonomous.gh = orig


if __name__ == '__main__':
    unittest.main()
