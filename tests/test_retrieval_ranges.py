import unittest

from structure.chunks import scene_chunks
from retrieval.ranges import searchable_ranges


class RetrievalRangeTests(unittest.TestCase):
    def test_search_uses_body_and_excludes_non_body_blocks(self):
        sections = [{
            'id': 'chapter', 'parent_id': None,
            'body_start_char': 100, 'body_end_char': 500,
            'excluded_ranges': [
                {'start': 100, 'end': 140},
                {'start': 420, 'end': 460},
            ],
        }]
        self.assertEqual(searchable_ranges(sections), [
            {'start': 140, 'end': 420}, {'start': 460, 'end': 500},
        ])

class PassageTests(unittest.TestCase):
    def test_scene_chunks_never_cross_scene_boundaries_and_keep_owner(self):
        text = ('Первая сцена. ' * 80) + ('Вторая сцена. ' * 80)
        boundary = len('Первая сцена. ' * 80)
        first = dict(id='s1', chapter_id='c1', start_char=0, end_char=boundary)
        second = dict(id='s2', chapter_id='c1', start_char=boundary, end_char=len(text))
        rows = scene_chunks(text, first) + scene_chunks(text, second)
        self.assertTrue(all(row['scene_id'] in {'s1', 's2'} for row in rows))
        self.assertTrue(all(row['end_char'] <= boundary for row in rows
                            if row['scene_id'] == 's1'))
        self.assertTrue(all(row['start_char'] >= boundary for row in rows
                            if row['scene_id'] == 's2'))
        self.assertFalse(any(row['start_char'] < boundary < row['end_char'] for row in rows))

if __name__ == '__main__':
    unittest.main()
