"""Детерминированные поисковые фрагменты и их индексация."""
from hashlib import sha256
import math
from typing import Protocol

from structure.graph_db.repository import get_repository
from structure.chunks import passages, scene_chunks

from .repository import IndexRepository


class Embedder(Protocol):
    # Меняйте model при изменении модели, размерности или способа подготовки текста.
    model: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def vectors(values, count):
    if len(values) != count:
        raise ValueError('Провайдер вернул неверное количество эмбеддингов')
    result = [[float(x) for x in value] for value in values]
    dimensions = {len(value) for value in result}
    if len(dimensions) != 1 or not next(iter(dimensions), 0):
        raise ValueError('Эмбеддинги должны иметь одинаковую ненулевую размерность')
    if any(not all(math.isfinite(x) for x in value) or not any(value) for value in result):
        raise ValueError('Эмбеддинг содержит нечисловые значения или нулевой вектор')
    return result


class IndexService:
    """Явная подготовка книги; не запускается инструментами поиска."""

    def __init__(self, book_id, repository=None, embedder=None):
        self.book_id = book_id
        self.repository = IndexRepository(repository or get_repository())
        self.embedder = embedder

    def index_book(self, *, min_chars=400, max_chars=1200, batch_size=32,
                   chapter_ids=None):
        """Подготовить все чанки; embeddings можно ограничить набором глав."""
        if type(batch_size) is not int or not 1 <= batch_size <= 256:
            raise ValueError('batch_size должен быть от 1 до 256')
        if self.embedder is not None and not self.embedder.model.strip():
            raise ValueError('Укажи стабильное имя embedding-модели')
        source, cached, source_scenes = self.repository.source(self.book_id)
        if not source_scenes:
            raise ValueError('Сначала построй NarrativeScene хотя бы для одной главы')
        selected = None if chapter_ids is None else set(chapter_ids)
        if selected is not None:
            if not selected or not all(isinstance(value, str) and value for value in selected):
                raise ValueError('chapter_ids: непустой список id глав')
            known = {scene['chapter_id'] for scene in source_scenes}
            unknown = sorted(selected - known)
            if unknown:
                raise ValueError(f'Для глав нет готовых NarrativeScene: {unknown}')
        # embedding_hash учитывает нарративный контекст чанка (narrative/annotate.py),
        # если он уже размечен; content_hash остаётся чистым хэшем текста чанка и не
        # трогается — это идентичность узла, а не ключ кэша эмбеддинга.
        contexts = source['contexts']
        rows = [row for scene in source_scenes
                for row in scene_chunks(source['text'], scene, min_chars, max_chars,
                                        source['body_ranges'])]
        embed_texts = {}
        for row in rows:
            context = contexts.get(row['id'], '')
            embed_text = f"{context}\n\n{row['text']}" if context else row['text']
            row['embedding_hash'] = sha256(embed_text.encode()).hexdigest()
            embed_texts[row['embedding_hash']] = embed_text
        scene_rows = []
        for scene in source_scenes:
            text = '\n'.join(value.strip() for value in (
                scene.get('title') or '', scene.get('summary') or '',
            ) if value.strip())
            digest = sha256(text.encode()).hexdigest()
            row = dict(id=scene['id'], text=text, content_hash=digest, embedding_hash=digest)
            if (scene.get('cached_hash') == digest and scene.get('cached_embedding')
                    and scene.get('cached_model')):
                cached.append(dict(hash=digest, embedding=scene['cached_embedding'],
                                   model=scene['cached_model']))
            scene_rows.append(row)
        models = {r['model'] for r in cached}
        model = self.embedder.model if self.embedder else (next(iter(models)) if len(models) == 1 else None)
        cache = {r['hash']: r['embedding'] for r in cached if r['model'] == model}
        all_rows = [*rows, *scene_rows]
        embedding_rows = [row for row in rows if selected is None or row['chapter_id'] in selected]
        embedding_rows += [row for row, scene in zip(scene_rows, source_scenes)
                           if selected is None or scene['chapter_id'] in selected]
        missing = list(dict.fromkeys(r['embedding_hash'] for r in embedding_rows
                                    if r['embedding_hash'] not in cache))
        texts = {**embed_texts, **{r['embedding_hash']: r['text'] for r in scene_rows}}
        embedded = 0
        if self.embedder:
            for start in range(0, len(missing), batch_size):
                keys = missing[start:start+batch_size]
                values = vectors(self.embedder.embed_documents([texts[k] for k in keys]), len(keys))
                cache.update(zip(keys, values))
                embedded += len(keys)
        for row in all_rows:
            if row['embedding_hash'] in cache:
                row.update(embedding=cache[row['embedding_hash']], embedding_model=model)
        present = [r['embedding'] for r in all_rows if 'embedding' in r]
        if present:
            vectors(present, len(present))
        generation = self.repository.replace_chunks(
            self.book_id, source, rows, scene_rows, model,
        )
        return dict(book_id=self.book_id, generation=generation, chunks=len(rows),
                    scenes=len(scene_rows), embedded_texts=embedded,
                    chunks_with_embeddings=sum('embedding' in row for row in rows),
                    scenes_with_embeddings=sum('embedding' in row for row in scene_rows),
                    embedding_model=model)
