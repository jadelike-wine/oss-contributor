import json
import sqlite3
import unittest

from issue_reassessment import enqueue


class ReassessmentQueueTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,repo TEXT,number INTEGER,state TEXT,holds TEXT)')
        self.db.execute('INSERT INTO tasks VALUES (?,?,?,?,?)',
                        ('task', 'Owner/Repo', 12, 'HOLD', json.dumps(['existing PR: example'])))
        self.db.commit()
        self.candidate = {'id': 'task', 'repo': 'owner/repo', 'number': 12,
                          'relations': [{'relation': 'scope_review'}]}

    def tearDown(self):
        self.db.close()

    def test_enqueue_is_repeatable_and_preserves_hold_and_completed_history(self):
        before = dict(self.db.execute('SELECT * FROM tasks').fetchone())
        with self.db:
            self.assertEqual(1, enqueue(self.db, [self.candidate], reason='recheck'))
            self.db.execute("UPDATE issue_refresh_queue SET status='completed',attempts=2")
            self.assertEqual(0, enqueue(self.db, [self.candidate], reason='recheck'))
        self.assertEqual(before, dict(self.db.execute('SELECT * FROM tasks').fetchone()))
        row = self.db.execute('SELECT * FROM issue_refresh_queue').fetchone()
        self.assertEqual(('completed', 2), (row['status'], row['attempts']))
        self.assertTrue(json.loads(row['reason'])['fresh_read_required'])

    def test_identity_mismatch_rolls_back_batch_and_terminal_work_is_not_queued(self):
        enqueue(self.db, [], reason='initialize')
        self.db.commit()
        with self.assertRaises(ValueError), self.db:
            enqueue(self.db, [self.candidate, {**self.candidate, 'number': 13}], reason='recheck')
        self.assertEqual(0, self.db.execute('SELECT count(*) FROM issue_refresh_queue').fetchone()[0])
        self.db.execute("UPDATE tasks SET state='PR_OPEN'")
        self.assertEqual(0, enqueue(self.db, [self.candidate], reason='recheck'))


if __name__ == '__main__':
    unittest.main()
