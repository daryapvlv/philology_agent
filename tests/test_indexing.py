"""IndexService.index_book: эмбеддинг чанка строится из narrative_context (если
есть) + text, а не только text (plan.md, блок D.1). content_hash остаётся чистым
хэшем текста чанка; отдельный embedding_hash — ключ кэша эмбеддинга."""
import unittest

from retrieval.indexing.service import IndexService

TEXT = 'Пётр Гринёв стоял у ворот крепости и смотрел на дорогу.'


class FakeIndexRepository:
    """Фейк на уровне IndexRepository (как FakeRepository в test_hybrid_search.py
    для SearchService) — без реального Neo4j."""

    def __init__(self, contexts=None, cached=None):
        self.contexts = contexts or {}
        self.cached = list(cached or [])
        self.replace_calls = []

    def source(self, _book_id):
        source = dict(text=TEXT, text_hash='h1', revision=1, generation='g1',
                      body_ranges=[dict(start=0, end=len(TEXT))], contexts=dict(self.contexts))
        scenes = [dict(id='scene1', chapter_id='chapter1', start_char=0, end_char=len(TEXT),
                       title='Глава 1', summary='Гринёв у ворот',
                       cached_hash=None, cached_embedding=None, cached_model=None)]
        return source, list(self.cached), scenes

    def replace_chunks(self, _book_id, _source, rows, scene_rows, model):
        self.replace_calls.append(dict(rows=[dict(r) for r in rows],
                                       scene_rows=[dict(r) for r in scene_rows], model=model))
        return 'generation-2'


class FakeEmbedder:
    model = 'test/model'

    def __init__(self):
        self.embedded_texts = []

    def embed_documents(self, texts):
        self.embedded_texts += list(texts)
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, _text):
        return [1.0, 0.0]


def service(repository, embedder=None):
    result = IndexService.__new__(IndexService)
    result.book_id = 'book'
    result.repository = repository
    result.embedder = embedder
    return result


class ContextAwareEmbeddingTests(unittest.TestCase):
    def test_cached_embedding_keyed_by_embedding_hash_is_reused(self):
        repository = FakeIndexRepository()
        index = service(repository, FakeEmbedder())
        index.index_book(min_chars=5, max_chars=1000)
        row = repository.replace_calls[0]['rows'][0]

        repository2 = FakeIndexRepository(cached=[
            dict(hash=row['embedding_hash'], embedding=[0.5, 0.5], model='test/model')])
        embedder2 = FakeEmbedder()
        index2 = service(repository2, embedder2)
        index2.index_book(min_chars=5, max_chars=1000)
        self.assertNotIn(TEXT, embedder2.embedded_texts)
        self.assertEqual(repository2.replace_calls[0]['rows'][0]['embedding'], [0.5, 0.5])


if __name__ == '__main__':
    unittest.main()
