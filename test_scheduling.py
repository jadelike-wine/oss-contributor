import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import contribute
from contribute import Pipeline
from autonomous import Autonomous


class RepositorySchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repos = ['QwenLM/qwen-code', 'google-gemini/gemini-cli', 'anomalyco/opencode']
        (self.root / 'config.json').write_text(json.dumps({
            'repositories': self.repos, 'max_analysis_per_tick': 1,
            'automation': {'enabled': True, 'max_tasks_per_tick': 1}}), encoding='utf-8')
        self.p = Pipeline(self.root)
        self.engine = Autonomous(self.p)
        self.ids = []
        for index, repo in enumerate(self.repos):
            key = self.p.hunt(repo, 'persistent candidate')
            self.ids.append(key)
            with self.p.db:
                self.p.db.execute('UPDATE tasks SET score=? WHERE id=?', (10 + index * 100, key))

    def tearDown(self):
        self.p.db.close()
        self.temp.cleanup()

    def dispatch(self, rounds):
        seen = []
        with patch.object(self.engine, 'advance', side_effect=lambda key: seen.append(self.p.task(key)['repo']) or 'PLAN'):
            for _ in range(rounds):
                self.engine.run()
        return seen

    def test_persistent_candidates_get_equal_turns_despite_scores_and_restart(self):
        first = self.dispatch(2)
        self.p.db.close()
        self.p = Pipeline(self.root)
        self.engine = Autonomous(self.p)
        self.assertEqual(first + self.dispatch(4), self.repos * 2)

    def test_larger_budget_interleaves_repositories_and_keeps_local_priority(self):
        best = self.p.hunt(self.repos[0], 'higher priority')
        with self.p.db:
            self.p.db.execute('UPDATE tasks SET score=1000 WHERE id=?', (best,))
        self.engine.options['max_tasks_per_tick'] = 4
        seen = []
        with patch.object(self.engine, 'advance', side_effect=lambda key: seen.append(key) or 'PLAN'):
            self.engine.run()
        self.assertEqual(seen, [best, self.ids[1], self.ids[2], self.ids[0]])

    def test_skips_empty_and_disallowed_repositories(self):
        self.p.config['repositories'] = self.repos[1:]
        with self.p.db:
            self.p.db.execute("UPDATE tasks SET state='HOLD' WHERE id=?", (self.ids[1],))
        self.assertEqual(self.dispatch(2), [self.repos[2]] * 2)

    def test_preview_and_busy_worker_do_not_consume_turn(self):
        self.dispatch(1)
        self.engine.candidates()
        self.engine.candidates()
        with self.p.lock('worker'):
            self.assertEqual(self.dispatch(1), [])
        self.assertEqual(self.dispatch(1), [self.repos[1]])

    def test_published_followup_keeps_priority_without_changing_research_budget(self):
        data = self.engine.load(self.ids[2])
        data['pr_number'] = 123
        self.engine.move(self.ids[2], data, 'PR_OPEN')
        seen = []
        with patch.object(self.engine, 'advance', side_effect=lambda key: seen.append(key) or ('PR_OPEN' if key == self.ids[2] else 'PLAN')):
            self.engine.run()
            self.assertEqual(seen, [self.ids[2], self.ids[0]])
            seen.clear()
            self.engine.run()
            self.assertEqual(seen, [self.ids[2], self.ids[1]])

    def test_failed_attempt_yields_next_turn(self):
        with patch.object(self.engine, 'advance', return_value='FAILED'):
            self.engine.run()
        self.assertEqual(self.dispatch(1), [self.repos[1]])

    def test_interrupted_attempt_persists_turn_before_dispatch(self):
        with patch.object(self.engine, 'advance', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.engine.run()
        self.p.db.close()
        self.p = Pipeline(self.root)
        self.engine = Autonomous(self.p)
        self.assertEqual(self.dispatch(1), [self.repos[1]])

    def test_existing_pr_repair_cannot_monopolize_research_slots(self):
        data = self.engine.load(self.ids[2])
        data['pr_number'] = 123
        self.engine.move(self.ids[2], data, 'REPAIR')
        self.assertEqual(self.dispatch(6), self.repos * 2)

    def test_configuration_reordering_resumes_after_repository_name(self):
        self.dispatch(1)
        self.p.config['repositories'] = [self.repos[1], self.repos[0], self.repos[2]]
        self.assertEqual(self.dispatch(1), [self.repos[2]])
        self.p.config['repositories'] = self.repos[:2]
        self.assertEqual(self.dispatch(1), [self.repos[0]])

    def test_legacy_tick_uses_persistent_rotation(self):
        self.p.config['automation']['enabled'] = False
        seen = []
        with patch('contribute.Pipeline', return_value=self.p), patch('sys.argv', ['contribute.py', 'tick', '--analyze']), \
                patch.object(self.p, 'scan', return_value=0), patch.object(self.p, 'followup', return_value=[]), \
                patch.object(self.p, 'analyze', side_effect=lambda key: seen.append(self.p.task(key)['repo'])):
            for _ in range(3):
                contribute.main()
        self.assertEqual(seen, self.repos)


if __name__ == '__main__':
    unittest.main()
