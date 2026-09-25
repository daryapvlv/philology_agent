import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from application.book_preparation import BookPreparationService
from llm.config import LLMConfig


class BookPreparationTests(unittest.TestCase):
    def setUp(self):
        # Стадия 8 проверяется отдельно (NarrativeStageTests); здесь флаг из
        # окружения разработчика (.envrc) не должен включать её в fake-pipeline.
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('NARRATIVE_LAYER_ENABLED', None)

    def test_pipeline_orders_scenes_chunks_entities_and_book_graph(self):
        events = []
        scenes = {
            'c1': [dict(id='s1', text_hash='h1', entity_extraction_status='ready',
                        entity_extraction_hash='h1')],
            'c2': [dict(id='s2', text_hash='h2')],
        }

        class Book:
            def __init__(self, *_):
                pass

            def outline(self):
                return {'sections': [dict(id='c1'), dict(id='c2')]}

            def sections_without_scene_text(self, _ids):
                return []

            def build_scenes(self, chapter_id, *_args, **_kwargs):
                events.append(('scenes', chapter_id))
                return {'scenes': {chapter_id: {'status': 'ready',
                                                'items': scenes[chapter_id]}}}

        class Entities:
            def __init__(self, *_):
                pass

            def extract_scene_mentions(self, scene_id, *_args, **_kwargs):
                events.append(('mentions', scene_id))

            def resolve_chapter(self, chapter_id, *_args, **_kwargs):
                events.append(('chapter', chapter_id))
                return {'chapter_id': chapter_id}

            def resolve_book(self, *_args, rebuild=False, **_kwargs):
                events.append(('book', rebuild))
                return {'graph': {'presence_edges': 1, 'next_edges': 1}}

            def reconcile_book(self, *_args, **_kwargs):
                raise AssertionError('reconciliation не запрашивался')

        class Index:
            def __init__(self, *_args, **_kwargs):
                pass

            def index_book(self, *, chapter_ids):
                events.append(('chunks', tuple(chapter_ids)))
                return {'chunks': 2}

        with (patch('application.book_preparation.BookService', Book),
              patch('application.book_preparation.EntityService', Entities),
              patch('application.book_preparation.IndexService', Index)):
            service = BookPreparationService(
                'book', object(), object(), LLMConfig(),
                embedder=SimpleNamespace(embed_query=lambda _text: [1.0]),
            )
            progress = []
            service.prepare(['c1', 'c2'], with_embeddings=True, full_rebuild=True,
                            progress=lambda *args: progress.append(args))

        self.assertEqual(events[:3], [
            ('scenes', 'c1'), ('scenes', 'c2'), ('chunks', ('c1', 'c2')),
        ])
        self.assertNotIn(('mentions', 's1'), events)  # актуальная extraction пропущена
        self.assertIn(('mentions', 's2'), events)
        self.assertEqual(events[-1], ('book', True))
        keys = list(dict.fromkeys(args[0] for args in progress))
        self.assertEqual(keys, ['embeddings_check', 'scenes', 'chunks', 'mentions',
                                'chapter_entities', 'book_entities'])
        self.assertIn(('scenes', 2, 2, 'c2'), progress)

    def test_book_mode_keeps_going_and_reports_only_failed_chapters(self):
        events = []

        class Book:
            def __init__(self, *_):
                pass

            def outline(self):
                return {'sections': [dict(id='c1'), dict(id='c2')]}

            def sections_without_scene_text(self, _ids):
                return []

            def build_scenes(self, chapter_id, *_args, **_kwargs):
                events.append(('scenes', chapter_id))
                if chapter_id == 'c1':
                    raise RuntimeError('provider rejected chapter')
                return {'scenes': {chapter_id: {'status': 'ready', 'items': [
                    dict(id='s2', text_hash='h2'),
                ]}}}

        class Entities:
            def __init__(self, *_):
                pass

            def extract_scene_mentions(self, scene_id, *_args, **_kwargs):
                events.append(('mentions', scene_id))

            def resolve_chapter(self, chapter_id, *_args, **_kwargs):
                events.append(('chapter', chapter_id))
                return {'chapter_id': chapter_id}

            def resolve_book(self, *_args, **_kwargs):
                events.append(('book',))
                return {'graph': {'presence_edges': 1, 'next_edges': 0}}

        index = SimpleNamespace(index_book=lambda **_: {'chunks': 1,
                                                         'chunks_with_embeddings': 0})
        with (patch('application.book_preparation.BookService', Book),
              patch('application.book_preparation.EntityService', Entities),
              patch('application.book_preparation.IndexService', lambda *_a, **_k: index)):
            result = BookPreparationService(
                'book', object(), object(), LLMConfig(), max_workers=2,
            ).prepare(['c1', 'c2'], continue_on_error=True)

        self.assertEqual(result['failed_chapter_ids'], ['c1'])
        self.assertEqual(result['completed_chapter_ids'], ['c2'])
        self.assertEqual(result['failures'][0].chapter_id, 'c1')
        self.assertIn(('mentions', 's2'), events)
        self.assertIn(('chapter', 'c2'), events)
        self.assertIn(('book',), events)


if __name__ == '__main__':
    unittest.main()
