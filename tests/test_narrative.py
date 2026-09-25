import json
from types import SimpleNamespace
import unittest

from llm.config import LLMConfig
from narrative import NarrativeService
from narrative.annotate import annotate_scene
from narrative.repository import NarrativeRepository


SCENE = dict(id='s1', chapter_id='c1', book_id='book', start_char=100, end_char=140,
            text='Гринёв вошёл в комнату. «Здравствуй», сказал он Швабрину.',
            text_hash='hash1')
CHUNKS = [
    dict(id='chunk1', start_char=100, end_char=123, scene_id='s1'),
    dict(id='chunk2', start_char=123, end_char=159, scene_id='s1'),
]
ENTITIES = [
    dict(id='e1', canonical_name='Пётр Гринёв', entity_type='person'),
    dict(id='e2', canonical_name='Швабрин', entity_type='person'),
]


def valid_reply():
    return {
        'chunks': [
            dict(chunk=1, context='Гринёв входит в комнату.', representation='narrator',
                speaker_entity_id=None, level='primary', embedded_kind=None),
            dict(chunk=2, context='Гринёв здоровается со Швабриным.',
                representation='direct_speech', speaker_entity_id='e1',
                level='primary', embedded_kind=None),
        ],
        'events': [
            dict(surface_text='Здравствуй', occurrence=1, gist='Гринёв поздоровался со Швабриным.',
                gist_roles='Один персонаж поздоровался с другим.', kind='change_of_state',
                modality='actual', participants=[dict(entity_id='e1', role='инициатор'),
                                                 dict(entity_id='e2', role='адресат')],
                from_location=None, to_location=None),
        ],
    }


class StageClient:
    """Фальшивый LLM для agent.judge: одна и та же схема ответа."""
    base_url = 'test://narrative'

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


class AnnotateSceneTests(unittest.TestCase):
    def test_valid_reply_is_parsed_without_ids(self):
        client = StageClient([valid_reply()])
        result = annotate_scene(client, LLMConfig(), SCENE, CHUNKS, ENTITIES,
                                cache_dir=None, usage=dict(calls=0, tokens=0, cache_hits=0))
        self.assertEqual(len(result['chunks']), 2)
        self.assertEqual(result['chunks'][1]['speaker_entity_id'], 'e1')
        self.assertEqual(len(result['events']), 1)
        event = result['events'][0]
        # 'Здравствуй' начинается в SCENE.text сразу после '[C2]' в разметке,
        # но offset здесь — локальный к самому тексту сцены, без меток.
        self.assertEqual(SCENE['text'][event['start_offset']:event['end_offset']], 'Здравствуй')
        self.assertEqual(len(client.calls), 1)

    def test_invalid_reply_gets_one_retry_with_error_text(self):
        bad = dict(valid_reply(), chunks=[dict(valid_reply()['chunks'][0], chunk=1)])  # без chunk 2
        replies = iter([bad, valid_reply()])
        client = StageClient(lambda _payload: next(replies))
        result = annotate_scene(client, LLMConfig(), SCENE, CHUNKS, ENTITIES,
                                cache_dir=None, usage=dict(calls=0, tokens=0, cache_hits=0))
        self.assertEqual(len(result['chunks']), 2)
        self.assertEqual(len(client.calls), 2)
        self.assertIn('2', client.calls[1]['errors'][0])

class FakeNarrativeRepository(NarrativeRepository):
    """Та же публичная поверхность, что и Neo4j-версия, поверх словаря в памяти —
    для теста идемпотентности не нужен реальный сервер."""

    def __init__(self):
        self.scenes = {SCENE['id']: dict(SCENE)}
        self.chunks = {SCENE['id']: [dict(c) for c in CHUNKS]}
        self.entities = {SCENE['id']: [dict(e) for e in ENTITIES]}
        self.events = {}

    def scene_for_annotation(self, _book_id, scene_id):
        scene = self.scenes[scene_id]
        if not scene.get('text') or not scene.get('text_hash'):
            raise ValueError('У сцены отсутствует исходный текст или его hash')
        return dict(scene), list(self.chunks[scene_id]), list(self.entities[scene_id])

    def annotation_status(self, _book_id, scene_id, method_hash):
        scene = self.scenes[scene_id]
        return (scene.get('narrative_status') == 'ready'
                and scene.get('narrative_hash') == scene.get('text_hash')
                and scene.get('narrative_method_hash') == method_hash)

    def replace_annotation(self, _book_id, scene, chunk_rows, event_rows, method_hash):
        current = self.scenes[scene['id']]
        if current['text_hash'] != scene['text_hash']:
            raise ValueError('Сцена изменилась; повтори нарративную разметку')
        by_id = {c['id']: c for c in self.chunks[scene['id']]}
        for row in chunk_rows:
            by_id[row['id']].update({
                'narrative_context': row['context'],
                'narrative_representation': row['representation'],
                'narrative_speaker_entity_id': row['speaker_entity_id'],
                'narrative_level': row['level'],
                'narrative_embedded_kind': row['embedded_kind'],
            })
        self.events[scene['id']] = list(event_rows)
        current.update(narrative_status='ready', narrative_hash=scene['text_hash'],
                       narrative_method_hash=method_hash)


class NarrativeServiceIdempotencyTests(unittest.TestCase):
    def test_second_run_makes_zero_llm_calls_and_keeps_the_same_data(self):
        service = NarrativeService.__new__(NarrativeService)
        service.book_id = 'book'
        service.repository = FakeNarrativeRepository()
        client = StageClient([valid_reply()])

        first = service.annotate_scene('s1', client, config=LLMConfig(), cache_dir=None)
        self.assertFalse(first['skipped'])
        self.assertEqual(first['chunks'], 2)
        self.assertEqual(first['events'], 1)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(all(chunk.get('narrative_context')
                            for chunk in service.repository.chunks['s1']))

        second = service.annotate_scene('s1', client, config=LLMConfig(), cache_dir=None)
        self.assertTrue(second['skipped'])
        self.assertEqual(second['usage']['calls'], 0)
        self.assertEqual(len(client.calls), 1)  # ни одного нового вызова модели

    def test_changed_scene_hash_requires_llm_call_again(self):
        service = NarrativeService.__new__(NarrativeService)
        service.book_id = 'book'
        service.repository = FakeNarrativeRepository()
        client = StageClient([valid_reply(), valid_reply()])
        service.annotate_scene('s1', client, config=LLMConfig(), cache_dir=None)
        service.repository.scenes['s1']['text_hash'] = 'hash2'
        second = service.annotate_scene('s1', client, config=LLMConfig(), cache_dir=None)
        self.assertFalse(second['skipped'])
        self.assertEqual(len(client.calls), 2)

if __name__ == '__main__':
    unittest.main()
