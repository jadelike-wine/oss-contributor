import sqlite3
import unittest

from telemetry import migrate, observe_pr


class AcceptanceTests(unittest.TestCase):
    def test_response_approval_ci_and_merge_remain_distinct(self):
        with sqlite3.connect(':memory:') as db:
            migrate(db)
            pr = {'number': 1, 'state': 'OPEN', 'headRefOid': 'head',
                  'ci': {'state': 'approval_required'}, 'reviewDecision': 'APPROVED'}
            human = {'id': 1, 'body': 'Thanks', 'author_association': 'MEMBER',
                     'user': {'type': 'User', 'login': 'maintainer'}}
            observe_pr(db, 'owner/repo', {**pr, 'comments': [human]})
            self.assertEqual(('approval_required', 1), db.execute('SELECT ci_state,maintainer_response FROM pr_observations').fetchone())
            self.assertEqual((0, None, 'OPEN'), db.execute('SELECT human_approval,merged_at,state FROM pr_acceptance_observations').fetchone())
            for commit, account, expected in [('old', 'User', 0), ('head', 'Bot', 0), ('head', 'User', 1)]:
                review = {**human, 'state': 'APPROVED', 'commit_id': commit, 'user': {'type': account, 'login': 'maintainer'}}
                observe_pr(db, 'owner/repo', {**pr, 'reviews': [review]})
                self.assertEqual(expected, db.execute('SELECT human_approval FROM pr_acceptance_observations').fetchone()[0])
            observe_pr(db, 'owner/repo', {**pr, 'headRefOid': 'new', 'reviews': [review]})
            self.assertEqual(0, db.execute('SELECT human_approval FROM pr_acceptance_observations').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
