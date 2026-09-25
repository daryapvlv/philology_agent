"""Нарративная разметка сцены: representation/level чанков, события книги,
связи между сценами (обзор сюжета) и между событиями
(retells/prefigures/causes/parallels)."""
import os

from llm.call_log import logger
from llm.config import LLMConfig
from structure.graph_db.repository import get_repository

from .annotate import METHOD_HASH, NarrativeAnnotationError, annotate_scene, event_id
from .config import NarrativeLinkConfig
from .links import (
    LINKS_METHOD_HASH, NarrativeLinkError, embed_events, enrich_events, resolve_links,
    scene_link_pairs, shortlist_pairs,
)
from .repository import NarrativeRepository
from .scene_links import SCENE_LINKS_METHOD_HASH, map_scene_links

__all__ = ['NarrativeAnnotationError', 'NarrativeLinkError', 'NarrativeService',
           'narrative_layer_enabled']


def narrative_layer_enabled():
    """Флаг NARRATIVE_LAYER_ENABLED: недоделанный новый слой не должен по
    умолчанию включаться в подготовку книги и ломать рабочий поиск."""
    return (os.getenv('NARRATIVE_LAYER_ENABLED') or '').strip().casefold() in {'1', 'true', 'yes'}


class NarrativeService:
    def __init__(self, book_id, repository=None):
        self.book_id = book_id
        self.repository = NarrativeRepository(repository or get_repository())

    def annotate_scene(self, scene_id, client, *, config=None,
                       cache_dir='.cache/narrative/annotation', force=False):
        """Разметить сцену; при неизменном тексте и методе разметки — 0 LLM-вызовов."""
        config = (LLMConfig.from_config(config) if config is not None
                 else LLMConfig()).for_stage('narrative')
        usage = {'calls': 0, 'tokens': 0, 'cache_hits': 0}
        if not force and self.repository.annotation_status(self.book_id, scene_id, METHOD_HASH):
            return {'book_id': self.book_id, 'scene_id': scene_id, 'skipped': True,
                    'chunks': 0, 'events': 0, 'skipped_events': [], 'usage': usage}
        scene, chunks, entities = self.repository.scene_for_annotation(self.book_id, scene_id)
        result = annotate_scene(client, config, scene, chunks, entities,
                                cache_dir=cache_dir, usage=usage)
        chunk_rows = [dict(id=chunk['id'], context=row['context'],
                           representation=row['representation'],
                           speaker_entity_id=row['speaker_entity_id'],
                           level=row['level'], embedded_kind=row['embedded_kind'])
                     for chunk, row in zip(chunks, result['chunks'])]
        event_rows = [dict(row, id=event_id(self.book_id, scene_id, scene['text_hash'], index),
                           book_id=self.book_id, scene_id=scene_id,
                           input_hash=scene['text_hash'], method_hash=METHOD_HASH)
                     for index, row in enumerate(result['events'])]
        skipped_events = result.get('skipped_events') or []
        if skipped_events:
            logger.warning(
                'Narrative annotation dropped %d/%d event(s) for scene=%s: %s',
                len(skipped_events), len(result['events']) + len(skipped_events), scene_id,
                '; '.join(skipped_events[:3]),
            )
        self.repository.replace_annotation(
            self.book_id, scene, chunk_rows, event_rows, METHOD_HASH)
        return {'book_id': self.book_id, 'scene_id': scene_id, 'skipped': False,
                'chunks': len(chunk_rows), 'events': len(event_rows),
                'skipped_events': skipped_events, 'usage': usage}

    def preparation_status(self, chapter_ids):
        return self.repository.preparation_status(self.book_id, chapter_ids, METHOD_HASH)

    def rebuild_scene_links(self, client, config, *, cache_dir, usage):
        """Обзор сюжета: SCENE_LINK между сценами книги по их сводкам
        (narrative.scene_links). Возвращает (записанные связи, порции без ответа)."""
        outline = self.repository.scene_outline(self.book_id)
        rows, failures = map_scene_links(client, config, outline, cache_dir=cache_dir,
                                         usage=usage)
        if failures:
            logger.warning('Scene map is partial for book=%s: %s', self.book_id,
                           '; '.join(failures))
        hashes = {scene['id']: scene['text_hash'] for scene in outline}
        rows = [dict(row, source_hash=hashes[row['source_id']],
                     target_hash=hashes[row['target_id']]) for row in rows]
        self.repository.replace_scene_links(self.book_id, rows, SCENE_LINKS_METHOD_HASH)
        return rows, failures

    def rebuild_links(self, client, *, config=None, embedder=None,
                      cache_dir='.cache/narrative/links', scene_links=True):
        """Полный пересчёт связей книги в два шага:

        1. обзор сюжета — связи между сценами: порции сцен на фоне краткого
           перечня всей книги (narrative.scene_links); порции без ответа — в
           scene_link_failures;
        2. связи между событиями: shortlist кодом (эмбеддинги gist_roles/gist с
           кэшем, общие участники, пересказы, уже найденные связи и лучшие пары
           событий из каждой связанной пары сцен), LLM батчами классифицирует
           только отобранные пары; связь сцен передаётся ей как гипотеза (hint).
        Без кандидатов — 0 LLM-вызовов на втором шаге."""
        config = (NarrativeLinkConfig.from_config(config) if config is not None
                 else NarrativeLinkConfig()).for_stage('narrative_links')
        usage = {'calls': 0, 'tokens': 0, 'cache_hits': 0}
        scene_rows, scene_failures = (
            self.rebuild_scene_links(client, config, cache_dir=cache_dir, usage=usage)
            if scene_links else ([], []))
        scenes, raw_events, participants, chunks = self.repository.book_events(self.book_id)
        events = enrich_events(scenes, raw_events, participants, chunks,
                               context_chars=config.context_chars)
        embedding = embed_events(events, embedder, field='gist_roles', target='embedding',
                                 batch_size=config.embedding_batch_size)
        if embedding['to_persist']:
            self.repository.save_event_embeddings(self.book_id, embedding['to_persist'],
                                                  field='gist_roles')
        story = embed_events(embedding['events'], embedder, field='gist',
                             target='story_embedding', batch_size=config.embedding_batch_size)
        if story['to_persist']:
            self.repository.save_event_embeddings(self.book_id, story['to_persist'], field='gist')
        embedding = dict(story, embedded=embedding['embedded'] + story['embedded'])
        anchored, hints = scene_link_pairs(embedding['events'], scene_rows,
                                           per_link=config.scene_anchor_pairs)
        pairs = []
        if len(embedding['events']) >= 2:
            pairs = shortlist_pairs(
                embedding['events'], top_k=config.top_k,
                similarity_threshold=config.similarity_threshold,
                limit_factor=config.shortlist_limit_factor,
                participant_bonus=config.participant_bonus,
                known=[*self.repository.link_pairs(self.book_id), *anchored],
            )
        result = {'book_id': self.book_id, 'events': len(events),
                  'scene_links': len(scene_rows), 'scene_link_failures': scene_failures,
                  'pairs': len(pairs), 'links': 0,
                  'embedded': embedding['embedded'], 'usage': usage}
        if not pairs:
            self.repository.replace_links(self.book_id, [], LINKS_METHOD_HASH)
            return result
        rows, decisions = resolve_links(client, config, pairs, cache_dir=cache_dir, usage=usage,
                                        hints=hints)
        self.repository.replace_links(self.book_id, rows, LINKS_METHOD_HASH)
        return dict(result, links=len(rows))
