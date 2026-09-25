"""Сквозные проверки без сети: разметка → сцены → правки → пересчёт."""
import json
import re
from types import SimpleNamespace
import unittest

from structure.config import BuildScenesConfig
from structure.graph_db.repository import content_block_rows
from structure.rules import assemble_structure, chapter_units
from structure.workflows.build_scenes.workflow import build_scenes, scene_batches, PROMPT

class FakeClient:
    base_url = 'test://model'

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply
        self.chat = SimpleNamespace(completions=self)

    def with_options(self, **kwargs):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.reply:
            data = self.reply(kwargs, len(self.calls))
        else:
            start, end = map(int, re.search(r'Основная часть \[(\d+), (\d+)\)', kwargs['messages'][1]['content']).groups())
            data = {'continues_previous': start > 0, 'scenes': [
                {'end_unit': end - 1, 'title': 'Эпизод', 'summary': 'Герой идёт домой.'},
            ]}
        return SimpleNamespace(usage=SimpleNamespace(total_tokens=100), choices=[
            SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps(data))),
        ])


class FakeEmbedder:
    model = "test-embedding"

    def __init__(self):
        self.texts = []

    def embed_documents(self, texts):
        self.texts.extend(texts)
        return [[float(len(text)), 1.0] for text in texts]


def section(start, end, level, role, **kwargs):
    value = dict(start=start, end=end, level=level, role=role,
                 title=None, title_position=None, source='system')
    value.update(kwargs)
    return value


def fixture():
    elements = [{'text': f'Фрагмент {i}. Следующее предложение.',
                 **({'type': 'Title'} if i in {0, 4} else {})} for i in range(8)]
    issues, rejected = [], []
    sections = assemble_structure([
        section(0, 8, 1, 'work'), section(0, 4, 2, 'chapter'), section(4, 8, 2, 'chapter'),
    ], elements, 'book', issues, rejected)
    assert not issues and not rejected
    return elements, dict(document_id='book', sections=sections, issues=[], rejected_sections=[],
                         source={'sha256': 'text-v1'})


class StructureTests(unittest.TestCase):
    def test_graph_content_blocks_keep_epigraph_body_and_notes_under_chapter(self):
        elements = [
            {"text": "Глава I"}, {"text": "Строка эпиграфа"},
            {"text": "Автор"}, {"text": "Основной текст."},
            {"text": "[1] Примечание."},
        ]
        chapter = {
            "id": "chapter", "parent_id": None, "start": 0, "end": 5,
            "title_position": 0, "body_start": 3, "body_end": 4,
        }
        blocks = [
            {"id": "epigraph", "section_id": "chapter", "role": "epigraph",
             "start": 1, "end": 3, "attribution_start": 2, "source": "model"},
            {"id": "notes", "section_id": "chapter", "role": "footnotes",
             "start": 4, "end": 5, "source": "rule"},
        ]
        rows = content_block_rows("book", [chapter], blocks, elements)
        self.assertEqual([row["role"] for row in rows],
                         ["epigraph", "body", "footnotes"])
        self.assertEqual(rows[0]["text"], "Строка эпиграфа")
        self.assertEqual(rows[0]["attribution"], "Автор")
        self.assertEqual(rows[1]["text"], "Основной текст.")
        self.assertEqual(rows[2]["text"], "[1] Примечание.")

    def test_long_chapter_continuation_and_exact_coverage(self):
        elements, result = fixture()
        config = BuildScenesConfig(max_input_chars=len(PROMPT) + 1470)
        units = chapter_units(elements, result['sections'][1])
        batches = list(scene_batches(units, 70))
        self.assertEqual(batches[-1][1], len(units))
        client = FakeClient()
        built = build_scenes(result, elements, result['sections'][1]['id'], client, config=config, cache_dir=None)
        layer = next(iter(built['scenes'].values()))
        self.assertEqual(layer['status'], 'ready', layer['issues'])
        self.assertGreater(len(client.calls), 1)
        self.assertEqual(len(layer['items']), 1)
        self.assertEqual(layer['items'][0]['end_char'], units[-1]['end_char'])
        self.assertTrue(all(sum(len(m['content']) for m in call['messages']) <= config.max_input_chars for call in client.calls))

    def test_invalid_boundary_not_silently_accepted(self):
        elements, result = fixture()
        client = FakeClient(lambda *_: {'continues_previous': False, 'scenes': [
            {'end_unit': 999, 'title': 'x', 'summary': 'x'},
        ]})
        chapter_id = result['sections'][1]['id']
        built = build_scenes(result, elements, chapter_id, client, cache_dir=None)
        self.assertEqual(built['scenes'][chapter_id]['status'], 'failed')
        self.assertEqual(built['scenes'][chapter_id]['items'], [])

    def test_embedding_failure_does_not_save_unembedded_scenes(self):
        elements, result = fixture()
        chapter_id = result['sections'][1]['id']

        class FailingEmbedder(FakeEmbedder):
            def embed_documents(self, texts):
                raise RuntimeError('embedding API unavailable')

        built = build_scenes(result, elements, chapter_id, FakeClient(),
                             cache_dir=None, embedder=FailingEmbedder())
        layer = built['scenes'][chapter_id]
        self.assertEqual(layer['status'], 'failed')
        self.assertEqual(layer['items'], [])
        self.assertIn('embedding API unavailable', layer['issues'][0])

if __name__ == '__main__':
    unittest.main()
