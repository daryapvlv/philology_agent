from copy import deepcopy
import unittest

from retrieval.service import SearchService


class MemorySessions:
    def __init__(self):
        self.rows = {}

    def save(self, search_id, state, _changed=None):
        state.setdefault('version', 0)
        self.rows[search_id] = deepcopy(state)

    def load(self, search_id):
        return deepcopy(self.rows[search_id])


class FakeEmbedder:
    model = 'test/model'

    def embed_query(self, _query):
        return [1.0, 0.0]


class FakeRepository:
    def __init__(self):
        self.last_scene_ids = None
        self.book = {
            'revision': 1, 'generation': 'g1', 'current': True,
            'model': 'test/model', 'chunk_count': 2, 'embedded_count': 2,
            'dimensions': 2, 'scene_count': 2, 'scene_embedded_count': 2,
        }
        self.sections = [
            dict(id='part', title='Часть', role='part', level=1, parent_id=None,
                 start_char=0, end_char=200, body_start_char=None, body_end_char=None,
                 excluded_ranges=[]),
            dict(id='chapter1', title='Глава 1', role='chapter', level=2, parent_id='part',
                 start_char=0, end_char=100, body_start_char=0, body_end_char=100,
                 excluded_ranges=[]),
            dict(id='chapter2', title='Глава 2', role='chapter', level=2, parent_id='part',
                 start_char=100, end_char=200, body_start_char=100, body_end_char=200,
                 excluded_ranges=[]),
        ]

    def scope(self, _book_id, _section_ids):
        return deepcopy(self.book), deepcopy(self.sections), [dict(start=0, end=200)]

    def entity_catalog(self, _book_id):
        return [
            dict(id='e1', canonical_name='Пётр Андреевич Гринёв',
                 aliases=['Гринёв', 'Гринева'], entity_type='person', scene_count=3),
            dict(id='e2', canonical_name='Алексей Иванович Швабрин',
                 aliases=['Швабрин'], entity_type='person', scene_count=2),
        ]

    def entity_ids(self, _book_id, ids):
        return list(ids)

    def entity_scenes(self, _book_id, ids, mode, _ranges, _limit):
        self.mode = mode
        if list(ids) == ['e1']:
            return [dict(id='s3', start_char=110, end_char=160)]
        return [dict(id='s1', start_char=10, end_char=60,
                     anchor_start_char=30, anchor_end_char=38)]

    def entities_for_scenes(self, _book_id, scene_ids, entity_types):
        self.entities_for_scenes_args = (scene_ids, entity_types)
        return ['e1'] if entity_types == ['person'] else []

    def scene_ids(self, _book_id, ids, _ranges):
        return list(ids)

    def scene_ranges(self, _book_id, scene_ids):
        catalog = {
            's1': dict(id='s1', start_char=10, end_char=60),
            's2': dict(id='s2', start_char=70, end_char=90),
            's3': dict(id='s3', start_char=110, end_char=160),
        }
        return [catalog[i] for i in scene_ids if i in catalog]

    def scenes_in_ranges(self, _book_id, ranges, _limit):
        catalog = [
            dict(id='s1', start_char=10, end_char=60),
            dict(id='s2', start_char=70, end_char=90),
            dict(id='s3', start_char=110, end_char=160),
        ]
        return [row for row in catalog if any(
            row['start_char'] < r['end'] and row['end_char'] > r['start'] for r in ranges)]

    def related_scenes(self, _book_id, _spans, scene_ids, _ranges, _hops, _limit):
        self.related_scene_ids = scene_ids
        return [dict(id='s2', start_char=70, end_char=90, distance=1)] if 's1' in scene_ids else []

    def entity_graph_status(self, _book_id):
        return dict(ready_scenes=3, covered_scenes=1)

    def narrative_links_status(self, _book_id):
        return False

    def words(self, _book_id, _ranges, _terms, target, _limit):
        if target != 'text':
            return []
        return [dict(id='c1', start_char=10, end_char=60, score=.5)]

    def linked_scenes(self, _book_id, spans, scene_ids, link_types, _ranges, _limit):
        self.linked_scenes_calls = getattr(self, 'linked_scenes_calls', []) + [list(link_types)]
        return list(getattr(self, 'scene_link_rows', []))

    def linked_events(self, _book_id, spans, scene_ids, link_types, _ranges, _limit):
        self.linked_events_args = (list(scene_ids), list(link_types))
        self.linked_events_calls = getattr(self, 'linked_events_calls', []) + [list(link_types)]
        if not scene_ids and 'parallels' in link_types and any(
                span['start'] < 40 and span['end'] > 0 for span in spans):
            return [dict(id='c3', start_char=110, end_char=160, link_type='parallels',
                         anchor_gist='Швабрин стоит у ворот', target_gist='Пугачёв стоит у ворот',
                         reason='та же ситуация у ворот')]
        if link_types == ['retells'] and 's1' in scene_ids:
            return [dict(id='s3', start_char=110, end_char=160, link_type='retells',
                         anchor_gist='Пётр пишет письмо родителям',
                         target_gist='Савельич пересказывает письмо')]
        return []

    def similar_chunks(self, _book_id, span, _ranges, limit):
        rows = [dict(id='p2', start_char=40, end_char=70, score=.8)]
        return [row for row in rows if not (row['start_char'] < span['end']
                                            and row['end_char'] > span['start'])][:limit]

    def entities_possibly_same(self, _book_id, ids):
        self.possibly_same_args = list(ids)
        return ['e2'] if 'e1' in ids else []

    passages = [dict(id='p1', start_char=0, end_char=40),
                dict(id='p2', start_char=40, end_char=70),
                dict(id='p3', start_char=70, end_char=200)]

    def semantic(self, _book_id, _ranges, _vector, _model, _limit):
        return [dict(id='p2', start_char=40, end_char=70, score=.9),
                dict(id='p1', start_char=0, end_char=40, score=.8)]

    def scene_summaries(self, _book_id, _ranges, _vector, _model, _limit):
        return [dict(id='s1', start_char=10, end_char=60, score=.95)]

    def passages_overlapping(self, _book_id, spans):
        scene_passages = {'s1': self.passages[:2], 's2': self.passages[2:],
                          's3': self.passages[2:]}
        return [dict(span_id=span['id'], **passage) for span in spans
                for passage in (list({row['id']: row for scene_id in span.get('scene_ids', [])
                                      for row in scene_passages.get(scene_id, [])}.values())
                                if span.get('scene_ids') else self.passages)
                if span.get('scene_ids') or (
                    passage['start_char'] < span['end'] and passage['end_char'] > span['start'])]

    def neighbors(self, _book_id, _scene_id, _before, _after, _ranges):
        return [dict(id='s0', start_char=0, end_char=10),
                dict(id='s2', start_char=70, end_char=120)]

    def excerpts(self, _book_id, rows, limit):
        return [dict(id=row['id'], text=MemoryBooks.text[
            row['start_char']:min(row['end_char'], row['start_char'] + limit)]) for row in rows]

    _scene_of_passage = {
        (0, 40): dict(scene_id='s1', scene_title='Глава 1', scene_summary='Гринёв приезжает в крепость.'),
        (40, 70): dict(scene_id='s1', scene_title='Глава 1', scene_summary='Гринёв приезжает в крепость.'),
        (70, 200): dict(scene_id='s2', scene_title='Глава 2', scene_summary='Ссора со Швабриным.'),
    }

    def chunk_context(self, _book_id, spans):
        text = MemoryBooks.text
        result = []
        for span in spans:
            meta = self._scene_of_passage.get(
                (span['start'], span['end']),
                dict(scene_id=None, scene_title=None, scene_summary=None))
            result.append(dict(span_id=span['id'], **meta,
                               prev_tail=text[max(0, span['start'] - 20):span['start']],
                               next_head=text[span['end']:span['end'] + 20]))
        return result


def service():
    result = SearchService.__new__(SearchService)
    result.book_id = 'book'
    result.repository = FakeRepository()
    result.embedder = FakeEmbedder()
    result.sessions = MemorySessions()
    result.books = MemoryBooks()
    return result


class MemoryBooks:
    # отрывки p1 = 0–40 («Первая фраза. Тут стоял Швабрин у ворот.»), p2 = 40–70
    text = 'Первая фраза. Тут стоял Швабрин у ворот. Хвост истории длится.' + ' z' * 80

    def read_text(self, _book_id, start, end, limit):
        stop = min(end, start + limit)
        return dict(book_id='book', start_char=start, end_char=stop,
                    text=self.text[start:stop], next_start=stop if stop < end else None)


class HybridSearchTests(unittest.TestCase):
    def test_semantic_text_channel_ranks_passages_and_summary_channel_ranks_scenes(self):
        search = service()
        result = search.search_semantic('конфликт')
        spans = {(item['start_char'], item['end_char']): set(item['sources'])
                 for item in result['items']}
        self.assertEqual(spans[(40, 70)], {'semantic:text:конфликт'})
        self.assertEqual(spans[(10, 60)], {'semantic:summary:конфликт'})

    def test_projection_turns_scene_hits_into_passages_with_inherited_scores(self):
        search = service()
        scene = search.get_scenes_for_entities(['e2'])
        words = search.search_words('швабрин')
        merged = search.merge_results([scene['search_id'], words['search_id']])
        projected = search.project_passages(merged['search_id'], query='критерий')
        by_span = {(item['start_char'], item['end_char']): item for item in projected['items']}
        # сцена 10–60 пересекает отрывки p1 (0–40) и p2 (40–70)
        self.assertEqual(set(by_span), {(0, 40), (40, 70)})
        self.assertIn('entities:intersection:e2', by_span[(0, 40)]['sources'])
        self.assertEqual(projected['query'], 'критерий')
        # якорь упоминания (30–38) сохраняется только в отрывке, который его содержит
        state = search.sessions.load(projected['search_id'])
        anchors = {(i['start_char'], i['end_char']): i.get('anchors', []) for i in state['items']}
        self.assertEqual(anchors[(0, 40)], [dict(start_char=30, end_char=38)])
        self.assertEqual(anchors[(40, 70)], [])

    def test_verdicts_store_statuses_and_minimal_sentence_quotes(self):
        search = service()
        found = search.search_words('швабрин')
        projected = search.project_passages(found['search_id'])
        ids = [item['id'] for item in projected['items']]
        quotes = search.apply_verdicts(projected['search_id'], [
            dict(candidate_id=ids[0], status='relevant', reason='Упомянут Швабрин',
                 evidence='стоял  ШВАБРИН'),
            dict(candidate_id=ids[1], status='rejected', reason='Не о том', evidence=''),
        ])
        self.assertEqual(len(quotes), 1)
        self.assertTrue(quotes[0]['verified'])
        self.assertIn('Швабрин', quotes[0]['text'])
        self.assertFalse(quotes[0]['text'].startswith('Первая фраза'))
        counts = search.review_counts(projected['search_id'])
        self.assertEqual((counts['relevant'], counts['rejected']), (1, 1))

if __name__ == '__main__':
    unittest.main()
