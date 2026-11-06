import copy
import json
import copy
import datetime as dt
import subprocess
import unittest
from unittest.mock import patch

from research_scope import alias_exact, hold_overlap, overlaps, pending, resolve_overlap, scope_identity, review_pending, report_pending
import test_conversion_runtime as runtime_fixtures
import test_autonomous as autonomous_fixtures


class ResearchScopeTests(unittest.TestCase):
    setUp = runtime_fixtures.RuntimeConversionTests.setUp
    tearDown = runtime_fixtures.RuntimeConversionTests.tearDown
    issue = runtime_fixtures.RuntimeConversionTests.issue

    def candidate(self, number, *, sha='a' * 40, extra=False, pr=None):
        key = self.issue(number)
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET source='hunt',number=NULL WHERE id=?", (key,))
        matrix = {'feature_id': str(number), 'target_sha': sha, 'user_scenario': 'Open demo with modern React',
                  'affected_files': ['src/content.js'], 'implementation_plan': ['Use createRoot'],
                  'validation_plan': ['Run demo regression'],
                  'evidence': [{'repo': 'owner/repo', 'path': 'src/content.js', 'sha': sha,
                                'quote': 'ReactDOM.render(<Demo />, root);', 'file_type': 'source'}]}
        if extra:
            matrix['affected_files'].append('src/index.jsx')
            matrix['implementation_plan'].append('Deduplicate CodeSandbox payload')
        with self.p.db:
            self.p.db.execute('INSERT INTO research_candidates VALUES (?,?,?,?,?,?,?,?,?)',
                             (str(number), key, 'owner/repo', 'bug', 'READY_TO_IMPLEMENT', 80,
                              json.dumps(matrix), '2026-11-01', '2026-11-01'))
        if pr:
            self.engine.store(key, {'stage': 'PR_OPEN', 'attempts': {}, 'pr_number': pr})
        return key, matrix

    def test_exact_pinned_scope_uses_alias_and_preserves_task_and_candidate_history(self):
        first, matrix = self.candidate(1)
        second, other = self.candidate(2)
        before = self.p.db.execute('SELECT count(*) FROM tasks').fetchone()[0]
        self.assertEqual(first, alias_exact(self.p.db, self.p.task(second), other))
        self.assertEqual(before, self.p.db.execute('SELECT count(*) FROM tasks').fetchone()[0])
        self.assertEqual(2, self.p.db.execute('SELECT count(*) FROM research_candidates').fetchone()[0])
        self.assertIsNone(alias_exact(self.p.db, self.p.task(first), matrix), 'Do not create alias cycles')
        self.assertNotIn(second, {r['id'] for r in self.engine.candidates()})

    def test_same_title_without_same_evidence_or_scope_cannot_alias(self):
        first, matrix = self.candidate(1)
        second, other = self.candidate(2, extra=True)
        self.assertIsNone(alias_exact(self.p.db, self.p.task(second), other))
        self.assertEqual(['src/index.jsx'], overlaps(self.p.db, self.p.task(second), other)[0]['current_unique_files'])
        other['evidence'][0]['quote'] = 'different source behavior'
        self.assertEqual([], overlaps(self.p.db, self.p.task(second), other))

    def test_published_pr_never_aliases_or_gets_partial_overlap_hold(self):
        first, matrix = self.candidate(1)
        second, other = self.candidate(2, pr=42)
        self.assertIsNone(alias_exact(self.p.db, self.p.task(second), other))
        third, other = self.candidate(3, extra=True, pr=43)
        with self.assertRaisesRegex(ValueError, 'maintenance'):
            hold_overlap(self.p.db, third, first, 'Shared createRoot implementation; unique payload work needs review.')

    def test_tdesign_partial_overlap_blocks_setup_until_explicit_current_scope_review(self):
        first, matrix = self.candidate(1)
        second, other = self.candidate(2, sha='b' * 40, extra=True)
        self.assertIsNone(alias_exact(self.p.db, self.p.task(second), other), 'Different SHA and partial scope must not merge')
        receipt = hold_overlap(self.p.db, second, first, 'Both replace ReactDOM.render; preserve and separately review the unique payload deduplication.')
        self.assertEqual(['src/index.jsx'], receipt['current_unique_files'])
        self.assertTrue(pending(self.engine.load(second)))
        with patch.object(self.engine, 'plan') as plan, patch.object(self.engine, 'judge', return_value={
                'approved': False, 'scope_decision': 'unknown', 'reason': 'The remaining independent behavior requires additional source evidence.'}):
            self.engine.advance(second)
        plan.assert_not_called()
        with patch('repository_catalog.research_pool', return_value=['owner/repo']), patch.object(self.engine, 'research') as research:
            # First task is already approved; only held task could otherwise run.
            data = self.engine.load(first)
            data['research'] = {'approved': True}
            self.engine.store(first, data)
            self.engine.research_batch()
        research.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'current scope'):
            resolve_overlap(self.p.db, second, 'Reviewed unique payload work independently of the shared mount fix.', 'stale')
        resolve_overlap(self.p.db, second, 'Reviewed unique payload work independently of the shared mount fix.', scope_identity('owner/repo', other))
        self.assertFalse(pending(self.engine.load(second)))
        self.assertEqual(receipt['shared_evidence'], self.engine.load(second)['scope_overlap_history'][0]['shared_evidence'])
        with self.assertRaisesRegex(ValueError, 'completed resolution'):
            hold_overlap(self.p.db, second, first, 'A repeated review of unchanged material must not overwrite the completed resolution.')
        self.assertFalse(pending(self.engine.load(second)))

    def test_automatic_scope_review_is_bounded_and_repeats_on_material_state_change(self):
        first, matrix = self.candidate(1)
        second, other = self.candidate(2, extra=True)
        hold_overlap(self.p.db, second, first, 'Shared mount change, distinct payload change needs scope review.')
        data = self.engine.load(second)
        with patch.object(self.engine, 'judge', return_value={
                'approved': False, 'scope_decision': 'reuse', 'reason': 'Wait for the shared mount fix before reviewing the remaining payload behavior.'}) as review:
            self.assertTrue(review_pending(self.engine, second, data))
            self.assertFalse(review_pending(self.engine, second, data))
            self.assertEqual(1, review.call_count)
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET state='FAILED' WHERE id=?", (first,))
        with patch.object(self.engine, 'judge', return_value={
                'approved': True, 'scope_decision': 'distinct',
                'reason': 'The original mount candidate has failed; this candidate retains independently useful payload work.',
                'remaining_scope': 'Re-plan the payload deduplication with its own regression, separately from any mount API replacement.'}):
            self.assertTrue(review_pending(self.engine, second, data))
        self.assertFalse(pending(data))
        self.assertEqual('PLAN', data['stage'])
        self.assertEqual(2, len(data['scope_overlap_reviews']))
        self.assertEqual([], report_pending(self.p.db))

    def test_inactive_candidate_cannot_create_new_partial_hold(self):
        first, matrix = self.candidate(1)
        second, other = self.candidate(2, extra=True)
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET state='SKIPPED' WHERE id=?", (first,))
        with self.assertRaisesRegex(ValueError, 'Inactive research'):
            hold_overlap(self.p.db, second, first, 'Shared source but the older candidate is no longer active.')

    def test_network_failure_retries_after_cooldown_then_completed_unknown_is_stable(self):
        first, matrix = self.candidate(1)
        second, other = self.candidate(2, extra=True)
        hold_overlap(self.p.db, second, first, 'Shared mount change, distinct payload change needs scope review.')
        data = self.engine.load(second)
        at = dt.datetime(2026, 11, 6, tzinfo=dt.timezone.utc)
        with patch.object(self.engine, 'judge', side_effect=ConnectionError('connection reset')) as review:
            self.assertTrue(review_pending(self.engine, second, data, at))
            self.assertFalse(review_pending(self.engine, second, data, at + dt.timedelta(seconds=299)))
            self.assertEqual(1, review.call_count)
        self.assertEqual('retryable_error', data['scope_overlap_reviews'][0]['decision'])
        self.assertEqual('network', data['scope_overlap_reviews'][0]['category'])
        with patch.object(self.engine, 'judge', return_value={'approved': False, 'scope_decision': 'unknown',
                'reason': 'More pinned source evidence is required to isolate the remaining behavior.'}) as review:
            self.assertTrue(review_pending(self.engine, second, data, at + dt.timedelta(seconds=301)))
            self.assertFalse(review_pending(self.engine, second, data, at + dt.timedelta(days=1)))
            self.assertEqual(1, review.call_count)

    def test_interrupted_started_and_format_timeout_failures_share_bounded_retry_limit(self):
        first, matrix = self.candidate(1)
        second, other = self.candidate(2, extra=True)
        hold_overlap(self.p.db, second, first, 'Shared mount change, distinct payload change needs scope review.')
        data = self.engine.load(second)
        at = dt.datetime(2026, 11, 6, tzinfo=dt.timezone.utc)
        with patch.object(self.engine, 'judge', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            review_pending(self.engine, second, data, at)
        self.assertEqual('started', data['scope_overlap_reviews'][0]['decision'])
        with patch.object(self.engine, 'judge', return_value={'approved': True, 'scope_decision': 'distinct',
                'reason': 'Claimed distinct scope without a remaining scope is an invalid verdict.'}):
            self.assertFalse(review_pending(self.engine, second, data, at + dt.timedelta(seconds=299)))
            self.assertTrue(review_pending(self.engine, second, data, at + dt.timedelta(seconds=301)))
        self.assertEqual('model_format', data['scope_overlap_reviews'][1]['category'])
        timeout = subprocess.TimeoutExpired(['hermes'], 240, stderr=b'provider stream stalled')
        with patch.object(self.engine, 'judge', side_effect=timeout) as review:
            self.assertTrue(review_pending(self.engine, second, data, at + dt.timedelta(seconds=902)))
            self.assertFalse(review_pending(self.engine, second, data, at + dt.timedelta(days=1)))
            self.assertEqual(1, review.call_count)
        self.assertEqual('timeout', data['scope_overlap_reviews'][-1]['category'])
        self.assertIn('provider stream stalled', data['scope_overlap_reviews'][-1]['error'])
        self.assertTrue(data['scope_overlap_reviews'][-1]['exhausted'])
        self.assertTrue(pending(data))


class ScopePlanningTests(unittest.TestCase):
    setUp = autonomous_fixtures.AutonomousTests.setUp
    tearDown = autonomous_fixtures.AutonomousTests.tearDown
    git = autonomous_fixtures.AutonomousTests.git
    model = autonomous_fixtures.AutonomousTests.model
    fake_gh = autonomous_fixtures.AutonomousTests.fake_gh
    fake_command = autonomous_fixtures.AutonomousTests.fake_command
    mocks = autonomous_fixtures.AutonomousTests.mocks

    def test_remaining_scope_reaches_planner_and_recipe_review(self):
        data = self.engine.load(self.key)
        data['scope_overlap_review'] = {'state': 'resolved', 'remaining_scope': 'Only deduplicate payload; exclude createRoot.',
                                        'reason': 'The mount API change belongs to the existing candidate.'}
        data['scope_overlap_history'] = [{'shared_evidence': [{'path': 'content.js', 'quote': 'ReactDOM.render()'}]}]
        with self.mocks():
            self.engine.plan(self.key, data)
            prompt = self.engine.model.call_args.args[2]
            packet = self.engine.judge.call_args.args[2]
        self.assertIn('Only deduplicate payload; exclude createRoot.', prompt)
        self.assertIn('ReactDOM.render()', prompt)
        self.assertEqual(data['scope_overlap_review'], packet['scope_overlap_review'])
        self.assertEqual(data['scope_overlap_history'], packet['scope_overlap_history'])

    def test_first_research_hold_distinct_then_research_regenerates_remaining_scope(self):
        from feature_matrix import build, persist
        from test_feature_matrix import matrix_answer
        from test_research import opportunity
        for name, text in [('CONTRIBUTING.md', 'Small bug fixes welcome.'), ('extra.py', 'EXTRA = 1')]:
            (self.work / name).write_text(text, encoding='utf8')
        self.git('add', '.')
        self.git('commit', '-qm', 'scope fixtures')
        self.base = self.git('rev-parse', 'HEAD')
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET source='hunt',number=NULL,base_sha=? WHERE id=?", (self.base, self.key))
        task = self.p.task(self.key)
        packet = {'schema_version': 2, 'target': {'repo': task['repo'], 'sha': self.base,
                  'files': {name: (self.work / name).read_text(encoding='utf8') for name in ('value.py', 'extra.py', 'CONTRIBUTING.md')}},
                  'peers': [], 'policy': {'CONTRIBUTING.md': 'Small bug fixes welcome.'}}
        broad = opportunity()
        broad['feature_matrix'] = matrix_answer(task, [])['feature_matrix']
        raw = broad['feature_matrix']
        raw.update(affected_files=['value.py', 'extra.py'], implementation_plan=['Fix value and independently repair extra'],
                   user_scenario='Correct both value and extra behavior')
        raw['feature_status'][task['repo']].update(status='partial', missing='Both output contracts require correction',
                                                  evidence=copy.deepcopy(broad['evidence']))
        prior = self.p.hunt('owner/repo', 'Prior value implementation')
        prior_answer = copy.deepcopy(broad)
        prior_answer['feature_matrix'].update(feature_key='prior-value-implementation', affected_files=['value.py'],
                                              implementation_plan=['Fix value only'])
        persist(self.engine, self.p.task(prior), prior_answer, build(prior_answer, task, packet),
                reviewed=True, duplicate_clear=True)
        narrowed = copy.deepcopy(broad)
        narrowed.update(scope='extra.py only; exclude the existing value implementation',
                        evidence=[{'repo': task['repo'], 'path': 'extra.py', 'quote': 'EXTRA = 1'}])
        narrowed['feature_matrix'].update(user_scenario='Correct extra independently of value', affected_files=['extra.py'],
                                         implementation_plan=['Repair extra only; exclude value'], validation_plan=['Test extra behavior independently'])
        narrowed['feature_matrix']['feature_status'][task['repo']].update(checked_paths=['extra.py'], evidence=copy.deepcopy(narrowed['evidence']))
        prompts, research_reviews = [], []
        def generate(key, purpose, prompt, edit=False):
            self.assertEqual('research', purpose)
            prompts.append(prompt)
            return copy.deepcopy(broad if len(prompts) == 1 else narrowed)
        def review(key, purpose, context):
            if purpose == 'scope-overlap-review':
                return {'approved': True, 'scope_decision': 'distinct', 'reason': 'The extra behavior has its own source and independent value.',
                        'remaining_scope': 'Repair extra.py only with independent tests; exclude the shared value.py change.'}
            self.assertEqual('research-review', purpose)
            research_reviews.append(context)
            if len(research_reviews) == 1:
                return {'approved': False, 'local_scope_overlap_task': prior,
                        'reason': 'The value.py work overlaps the prior candidate; retain extra.py for independent review.'}
            self.assertEqual('resolved', context['scope_overlap_review']['state'])
            self.assertEqual(['extra.py'], context['opportunity']['feature_matrix']['affected_files'])
            return {'approved': True, 'reason': 'The revised extra-only scope is independent and satisfies all research gates.'}
        with self.mocks(), patch('research.collect', return_value=packet), patch.object(self.engine, 'duplicates', return_value=True), \
                patch.object(self.engine, 'model', side_effect=generate), patch.object(self.engine, 'judge', side_effect=review):
            self.engine.research(self.key, self.engine.load(self.key))
            data = self.engine.load(self.key)
            self.assertTrue(pending(data))
            self.assertNotIn('research', data)
            self.assertTrue(review_pending(self.engine, self.key, data))
            self.engine.research(self.key, self.engine.load(self.key))
        data = self.engine.load(self.key)
        self.assertEqual(2, len(prompts), 'Resolved scope must regenerate rather than reuse the broad pending answer')
        self.assertIn('RESOLVED LOCAL SCOPE', prompts[1])
        self.assertIn('exclude the shared value.py change', prompts[1])
        self.assertTrue(data['research']['approved'])
        self.assertFalse(pending(data))
        self.assertEqual(['extra.py'], data['research']['opportunity']['feature_matrix']['affected_files'])
        self.assertEqual(1, len(data['research_scope_revision_history']))
        self.assertNotIn('research_pending', data)
        self.assertEqual([], self.created_issues)
        self.assertEqual([], self.created_prs)


if __name__ == '__main__':
    unittest.main()
