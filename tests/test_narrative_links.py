import json
from types import SimpleNamespace
import unittest

from narrative import NarrativeService
from narrative.config import NarrativeLinkConfig
from narrative.links import classify_links, shortlist_pairs
from narrative.scene_links import map_scene_links
from narrative.repository import NarrativeRepository


SCENES = [
    dict(id='s1', title='Глава 1, отъезд', text='Пётр простился с Машей у ворот дома.',
        start_char=1000),
    dict(id='s2', title='Глава 2, письмо', text='Савельич прочёл письмо про их прощание вслух.',
        start_char=2000),
]
EVENTS = [
    dict(id='ev1', scene_id='s1', start_offset=0, end_offset=20,
        gist='Пётр простился с Машей.', gist_roles='герой простился с возлюбленной',
        kind='change_of_state', modality='actual'),
    dict(id='ev2', scene_id='s2', start_offset=13, end_offset=46,
        gist='Савельич пересказал прощание Петра и Маши.',
        gist_roles='слуга пересказал прощание героя с возлюбленной',
        kind='process', modality='reported'),
]

class StageClient:
    """Тот же фальшивый LLM-клиент, что и у narrative annotate/entities reconciliation."""
    base_url = 'test://narrative-links'

    def __init__(self, replies):
        self.replies = iter(replies) if not callable(replies) else replies
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    def with_options(self, **_):
        return self

    def create(self, **kwargs):
        payload = json.loads(kwargs['messages'][1]['content'])
        self.calls.append(payload)
        reply = self.replies(payload) if callable(self.replies) else next(self.replies)
        return SimpleNamespace(
            usage=SimpleNamespace(total_tokens=100),
            choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(
                content=json.dumps(reply, ensure_ascii=False)))])



class ShortlistPairsTests(unittest.TestCase):
    @staticmethod
    def event(id_, scene_id, *, embedding=None, participants=(), modality='actual',
              chunk_level=None, chunk_representation=None):
        row = dict(id=id_, scene_id=scene_id, participant_ids=list(participants),
                  modality=modality, chunk_level=chunk_level,
                  chunk_representation=chunk_representation)
        if embedding is not None:
            row['embedding'] = embedding
        return row

    def test_similar_events_from_different_scenes_are_paired(self):
        events = [
            self.event('a', 's1', embedding=[1.0, 0.0]),
            self.event('b', 's2', embedding=[0.99, 0.14]),
        ]
        pairs = shortlist_pairs(events, top_k=4, similarity_threshold=0.9,
                                limit_factor=3.0, participant_bonus=0.05)
        self.assertEqual({(left['id'], right['id']) for left, right in pairs}, {('a', 'b')})

class ClassifyLinksTests(unittest.TestCase):
    @staticmethod
    def pair(left_id, right_id):
        left = dict(id=left_id, gist=f'gist {left_id}', context=f'context {left_id}',
                   scene_title=f'scene {left_id}')
        right = dict(id=right_id, gist=f'gist {right_id}', context=f'context {right_id}',
                    scene_title=f'scene {right_id}')
        return left, right

    def test_decisions_without_a_valid_type_default_to_none(self):
        # Связь не угадывается: без явного допустимого type — 'none'. Дубликат
        # pair_id — первая запись с допустимым type, а не более поздняя.
        cases = {
            'pair missing from reply': (lambda pair_id: {'decisions': []}, 'none'),
            'unknown type': (
                lambda pair_id: {'decisions': [{'pair_id': pair_id, 'type': 'echoes',
                    'direction': None, 'confidence': 0.5, 'reason': 'x'}]}, 'none'),
            'duplicate pair_id keeps the first valid row': (
                lambda pair_id: {'decisions': [
                    {'pair_id': pair_id, 'type': 'retells', 'direction': None,
                     'confidence': 0.9, 'reason': 'первое'},
                    {'pair_id': pair_id, 'type': 'causes', 'direction': 'left_to_right',
                     'confidence': 0.9, 'reason': 'второе'},
                ]}, 'retells'),
        }
        for description, (build_reply, expected_type) in cases.items():
            with self.subTest(description=description):
                pairs = [self.pair('a', 'b')]
                client = StageClient(lambda payload: build_reply(payload['pairs'][0]['pair_id']))
                decisions = classify_links(client, NarrativeLinkConfig(model='m'), pairs,
                                          cache_dir=None)
                self.assertEqual(decisions[0]['type'], expected_type)

class FakeLinkRepository(NarrativeRepository):
    """Та же публичная поверхность NarrativeRepository, что и у Neo4j-версии,
    поверх словарей в памяти — по образцу FakeNarrativeRepository (test_narrative.py)."""

    def __init__(self, scenes, events, participants, chunks):
        self._scenes = scenes
        self._events = {event['id']: dict(event) for event in events}
        self._participants = dict(participants)
        self._chunks = chunks
        self.saved_embeddings = []
        self.links = None

    def book_events(self, _book_id):
        return (list(self._scenes), [dict(event) for event in self._events.values()],
                dict(self._participants), list(self._chunks))

    def save_event_embeddings(self, _book_id, rows, field='gist_roles'):
        self.saved_embeddings.extend(dict(row, field=field) for row in rows)
        for row in rows:
            self._events[row['id']].update({
                f'{field}_embedding': row['embedding'], f'{field}_embedding_model': row['model'],
                f'{field}_embedding_hash': row['hash']})

    def replace_links(self, _book_id, link_rows, _method_hash):
        self.links = list(link_rows)

    def link_pairs(self, _book_id):
        return [(row['source_id'], row['target_id']) for row in self.links or []
                if 'source_id' in row]

    outline = ()
    scene_links = None

    def scene_outline(self, _book_id):
        """Без явно заданного перечня обзор сюжета не делает вызовов (< 2 сцен)."""
        return [dict(row) for row in self.outline]

    def replace_scene_links(self, _book_id, link_rows, _method_hash):
        self.scene_links = list(link_rows)


class SceneLinkParsingTests(unittest.TestCase):
    def test_fenced_json_is_read_and_a_failed_group_leaves_a_partial_map(self):
        scenes = [dict(id=f's{i}', title=f't{i}', summary='s') for i in range(1, 5)]

        class Replies(StageClient):
            def create(self, **kwargs):
                reply = super().create(**kwargs)
                focus = json.loads(kwargs['messages'][1]['content'])['focus'][0]['n']
                content = reply.choices[0].message.content
                reply.choices[0].message.content = (
                    f'```json\n{content}\n```' if focus == 1 else 'не JSON')
                return reply

        client = Replies(lambda payload: {'links': [dict(a=1, b=4, type='parallels',
                                                         reason='повтор')]})
        rows, failures = map_scene_links(client, NarrativeLinkConfig(model='m', scene_focus_size=2),
                                         scenes, cache_dir=None)
        self.assertEqual([(r['source_id'], r['target_id']) for r in rows], [('s1', 's4')])
        # Вторая порция (сцены 3–4) дважды вернула не JSON: повтор, затем отказ.
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(len(failures), 1)
        self.assertIn('3–4', failures[0])

class SceneLinkAnchoringTests(unittest.TestCase):
    def test_rebuild_writes_scene_links_and_checks_their_events_with_the_hint(self):
        scenes = [dict(SCENES[0], summary='прощание'), dict(SCENES[1], summary='письмо')]
        repository = FakeLinkRepository(scenes, EVENTS, {}, [])
        repository.outline = [dict(id=s['id'], title=s['title'], summary=s['summary'],
                                   text_hash='h-' + s['id'], chapter='', characters=[])
                              for s in scenes]

        def reply(payload):
            if 'focus' in payload:
                return {'links': [dict(a=1, b=2, type='retells', direction='b_to_a',
                                       basis='explicit', reason='письмо о прощании')]}
            return {'decisions': [dict(pair_id=row['pair_id'], type='retells',
                                       direction='right_to_left', basis='explicit',
                                       reason='письмо пересказывает прощание')
                                  for row in payload['pairs']]}

        client = StageClient(reply)
        service = NarrativeService.__new__(NarrativeService)
        service.book_id, service.repository = 'book', repository
        result = service.rebuild_links(client, config=NarrativeLinkConfig(model='m'),
                                       cache_dir=None)
        self.assertEqual(result['scene_links'], 1)
        [scene_link] = repository.scene_links
        self.assertEqual((scene_link['source_id'], scene_link['target_id'],
                          scene_link['source_hash']), ('s2', 's1', 'h-s2'))
        [pair] = client.calls[1]['pairs']
        self.assertEqual(pair['hint'], 'пересказ между сценами: письмо о прощании')
        [link] = repository.links
        self.assertEqual((link['source_id'], link['target_id'], link['type'], link['basis']),
                         ('ev2', 'ev1', 'retells', 'explicit'))


if __name__ == '__main__':
    unittest.main()
