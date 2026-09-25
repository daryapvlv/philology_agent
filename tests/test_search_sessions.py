from pathlib import Path
import tempfile
import unittest

from infrastructure.sqlite.search_sessions import SqliteSearchSessions
from structure.graph_db.repository import ConflictError


def search_state():
    return {
        'scope': {'revision': 3, 'generation': 'index-v1'},
        'section_ids': None,
        'ranges': [{'start': 0, 'end': 10}],
        'items': [{
            'id': '0:10', 'start_char': 0, 'end_char': 10,
            'viewed': False, 'context_read': False,
            'review_status': 'unreviewed', 'reason': '',
            'selected': False, 'scores': {}, 'scene_ids': [],
        }],
        'query': 'return', 'warnings': [], 'truncated_channels': [],
        'cursor': 0,
    }


class SqliteSearchSessionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / 'philology.sqlite3'
        self.sessions = SqliteSearchSessions(self.database, 'book-1', 'chat-1')
        self.search_id = 'b' * 32

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_recent_and_compare_and_swap(self):
        state = search_state()
        self.sessions.save(self.search_id, state)
        self.assertEqual(state['version'], 0)

        first = self.sessions.load(self.search_id)
        stale = self.sessions.load(self.search_id)
        first['items'][0]['viewed'] = True
        self.sessions.save(self.search_id, first)

        loaded = self.sessions.load(self.search_id)
        self.assertEqual(loaded['version'], 1)
        self.assertTrue(loaded['items'][0]['viewed'])
        recent = self.sessions.recent(current={'revision': 3, 'generation': 'index-v1'})
        self.assertEqual(recent[0]['search_id'], self.search_id)
        self.assertTrue(recent[0]['current'])
        self.assertEqual(recent[0]['viewed'], 1)
        with self.assertRaises(ConflictError):
            self.sessions.save(self.search_id, stale)

if __name__ == '__main__':
    unittest.main()
