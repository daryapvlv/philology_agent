"""Сценарии UI/агента без JSON-файлов; отдельный integration test проверяет Cypher."""
from copy import deepcopy
import unittest

from test_structure import fixture, FakeClient
from structure.graph_db.repository import ConflictError
from structure.rules import validate_snapshot
from structure.application import BookService

class MemoryRepository:
    def __init__(self):
        self.elements, self.result = fixture()
        self.result.update(revision=0, book_title='Тестовая книга')

    def load(self, book_id):
        if book_id != self.result['document_id']:
            raise ValueError('Нет книги')
        return deepcopy(self.result), deepcopy(self.elements)

    def save(self, result, expected_revision):
        if self.result['revision'] != expected_revision:
            raise ConflictError('Конфликт версий')
        validate_snapshot(result, self.elements)
        self.result = deepcopy(result)
        self.result['revision'] = expected_revision + 1
        return self.result['revision']


class BookServiceTests(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepository()
        self.service = BookService('book', self.repo)
        self.chapter_id = self.repo.result['sections'][1]['id']
        self.client = FakeClient()
        self.service.build_scenes(self.chapter_id, self.client, cache_dir=None)

    def test_split_move_merge_and_edit_with_exact_coverage(self):
        result, elements = self.service.snapshot()
        original = result['scenes'][self.chapter_id]['items'][0]
        split = self.service.update_scene(self.chapter_id, original['id'], 'split', result['revision'], boundary=30)
        items = split['scenes'][self.chapter_id]['items']
        self.assertEqual(len(items), 2)
        right_id = items[1]['id']
        moved = self.service.update_scene(self.chapter_id, original['id'], 'move_boundary', split['revision'], boundary=35)
        self.assertEqual(moved['scenes'][self.chapter_id]['items'][1]['start_char'], 35)
        self.assertEqual(moved['scenes'][self.chapter_id]['items'][1]['id'], right_id)
        merged = self.service.update_scene(self.chapter_id, original['id'], 'merge_next', moved['revision'])
        self.assertEqual(len(merged['scenes'][self.chapter_id]['items']), 1)
        final = self.service.update_scene(self.chapter_id, original['id'], 'edit', merged['revision'], title='Домой', summary='Возвращение героя')
        scene = final['scenes'][self.chapter_id]['items'][0]
        self.assertEqual((scene['start_char'], scene['end_char']), (original['start_char'], original['end_char']))
        self.assertFalse(scene['description_stale'])
        self.assertEqual(scene['source'], 'human')

    def test_human_edits_survive_rebuild_unless_explicitly_replaced(self):
        result, _ = self.service.snapshot()
        scene = result['scenes'][self.chapter_id]['items'][0]
        self.service.update_scene(self.chapter_id, scene['id'], 'edit', result['revision'], title='Ручное название')
        calls = len(self.client.calls)
        preserved = self.service.build_scenes(self.chapter_id, self.client, cache_dir=None)
        self.assertEqual(preserved['scenes'][self.chapter_id]['items'][0]['title'], 'Ручное название')
        self.assertEqual(len(self.client.calls), calls)
        with self.assertRaises(ValueError):
            self.service.build_scenes(self.chapter_id, self.client, force=True, cache_dir=None)
        regenerated = self.service.build_scenes(self.chapter_id, self.client, force=True, replace_human=True, cache_dir=None)
        self.assertFalse(regenerated['scenes'][self.chapter_id].get('human_edited', False))

    def test_mutations_reject_old_revision(self):
        with self.assertRaises(ConflictError):
            self.service.update_section(self.chapter_id, {'end': 3}, -1)

if __name__ == '__main__':
    unittest.main()
