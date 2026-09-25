"""Application-фасад extraction и resolution сущностей книги."""
import json

from llm.config import LLMConfig
from llm.requests import request_json
from structure.graph_db.repository import get_repository

from .extractor import PersistentEntityExtractor
from .chapter_resolution import ChapterEntityResolver
from .book_resolution import BookEntityResolver
from .prompt import RESOLUTION_PROMPT
from .repository import EntityRepository
from .workflow import parse_mentions, parse_references, parse_resolutions


def _stage_config(config, stage):
    """config уже настроен на стадию (LLMConfig.for_stage), если задан
    LLM_<STAGE>_MODEL/LLM_<STAGE>_REASONING_EFFORT — иначе общий model."""
    return (LLMConfig.from_config(config) if config is not None else LLMConfig()).for_stage(stage)


def _content(raw, operation):
    if raw is None:
        raise ValueError(f"Бюджет {operation} исчерпан")
    if raw["finish_reason"] != "stop":
        raise ValueError(f"Ответ {operation} оборван")
    return json.loads(raw["content"])


class EntityService:
    def __init__(self, book_id, repository=None):
        self.book_id = book_id
        self.repository = EntityRepository(repository or get_repository())

    def extract_scene_mentions(self, scene_id, client, *, config=None,
                               cache_dir=".cache/entities/extraction"):
        config = LLMConfig.from_config(config) if config is not None else LLMConfig()
        scene = self.repository.scene(self.book_id, scene_id)
        chunks = self.repository.scene_chunks(self.book_id, scene_id)
        if not chunks:
            raise ValueError(
                'У сцены нет TextChunk; сначала выполни этап структуры/чанков'
            )
        extracted = PersistentEntityExtractor(
            client, config=config, cache_dir=cache_dir,
        ).extract(scene, chunks=chunks)
        mentions = parse_mentions({"mentions": extracted["mentions"]},
                                  book_id=self.book_id, scene=scene)
        references = parse_references(
            extracted["references"], book_id=self.book_id, scene=scene,
            normalized_mentions=extracted["mentions"], mentions=mentions,
        )
        self.repository.replace_mentions(self.book_id, scene, mentions, references)
        return {"book_id": self.book_id, "scene_id": scene_id,
                "mentions": mentions, "references": references,
                "rejected_mentions": extracted["rejected_mentions"],
                "rejected_references": extracted["rejected_references"],
                "usage": extracted["usage"]}

    def resolve_mentions(self, client, *, scene_id=None, config=None,
                         cache_dir=".cache/entities/resolution"):
        config = LLMConfig.from_config(config) if config is not None else LLMConfig()
        mentions = self.repository.mentions(self.book_id, scene_id, unresolved_only=True)
        entities = self.repository.entities(self.book_id)
        if not mentions:
            return {"book_id": self.book_id, "resolved": 0,
                    "created_entities": 0, "unresolved": 0,
                    "usage": {"calls": 0, "tokens": 0, "cache_hits": 0}}
        usage = {"calls": 0, "tokens": 0, "cache_hits": 0}
        payload = json.dumps({"mentions": mentions, "entities": entities}, ensure_ascii=False)
        raw = request_json(client, config, RESOLUTION_PROMPT, payload, cache_dir, usage)
        resolutions, created = parse_resolutions(
            _content(raw, "resolution"), book_id=self.book_id,
            mentions=mentions, entities=entities,
        )
        self.repository.apply_resolutions(self.book_id, resolutions, created)
        return {"book_id": self.book_id,
                "resolved": sum(row["entity_id"] is not None for row in resolutions),
                "created_entities": len(created),
                "unresolved": sum(row["entity_id"] is None for row in resolutions),
                "usage": usage}

    def resolve_chapter(self, chapter_id, client, *, config=None,
                        cache_dir=".cache/entities/chapter-resolution"):
        scenes, mentions, references = self.repository.chapter_evidence(
            self.book_id, chapter_id,
        )
        resolved = ChapterEntityResolver(
            client, config=_stage_config(config, 'chapter_resolution'), cache_dir=cache_dir,
        ).resolve(book_id=self.book_id, chapter_id=chapter_id,
                  scenes=scenes, mentions=mentions, references=references)
        source = sorted(({"id": mention["id"],
                          "scene_text_hash": mention["scene_text_hash"]}
                         for mention in mentions), key=lambda row: row["id"])
        self.repository.replace_chapter_entities(
            self.book_id, chapter_id, source, resolved["entities"],
        )
        return {"book_id": self.book_id, "chapter_id": chapter_id, **resolved}

    def chapter_entities(self, chapter_id):
        return self.repository.chapter_entities(self.book_id, chapter_id)

    def resolve_book(self, client, *, config=None,
                     cache_dir=".cache/entities/book-resolution", rebuild=False):
        if rebuild:
            pending, existing = self.repository.book_resolution_evidence(
                self.book_id, include_resolved=True,
            )
        else:
            pending, existing = self.repository.book_resolution_evidence(self.book_id)
        if rebuild:
            existing = []
        if not pending:
            graph = self.repository.rebuild_graph(self.book_id)
            return {"book_id": self.book_id, "entities": existing, "decisions": [],
                    "graph": graph,
                    "usage": {"calls": 0, "tokens": 0, "cache_hits": 0}}
        resolver = BookEntityResolver(
            client, config=_stage_config(config, 'book_resolution'), cache_dir=cache_dir,
        )
        resolved = resolver.resolve(
            book_id=self.book_id, chapter_entities=pending, book_entities=existing,
            apply_batch=(None if rebuild else lambda sources, decisions, entities:
                self.repository.apply_book_resolution_batch(
                    self.book_id, sources, decisions, entities,
                )),
        )
        if rebuild:
            self.repository.replace_book_resolution(
                self.book_id, pending, resolved["decisions"], resolved["entities"],
            )
        graph = self.repository.rebuild_graph(self.book_id)
        return {"book_id": self.book_id, **resolved, "graph": graph}

    def reconcile_book(self, client, *, config=None,
                       cache_dir=".cache/entities/book-reconciliation"):
        """Повторно сверить уже резолвленные BookEntity (см. BookEntityResolver.reconcile) —
        отдельный шаг от resolve_book, ничего не меняет, если merge не нашлось.

        Пары с решением UNCERTAIN сохраняются как POSSIBLY_SAME {score}: score —
        та же эвристическая оценка, что уже отобрала пару в shortlist (см.
        book_reconciliation_pairs), решение о типе связи по-прежнему за LLM."""
        entities = self.repository.book_entities(self.book_id)
        resolver = BookEntityResolver(
            client, config=_stage_config(config, 'book_reconciliation'), cache_dir=cache_dir)
        result = resolver.reconcile(book_id=self.book_id, entities=entities)
        for merge in result["merges"]:
            self.repository.merge_book_entities(
                self.book_id, merge["survivor_id"], merge["loser_ids"], merge["entity"],
            )
        uncertain = [
            {"left_entity_id": row["left_entity_id"], "right_entity_id": row["right_entity_id"],
             "score": row["score"]}
            for row in result["decisions"]
            if row["decision"] == "UNCERTAIN" and row.get("score") is not None
        ]
        if uncertain:
            self.repository.save_possibly_same(self.book_id, uncertain)
        graph = self.repository.rebuild_graph(self.book_id) if result["merges"] else None
        return {"book_id": self.book_id, **result, "graph": graph}

    def book_entities(self):
        return self.repository.book_entities(self.book_id)

    def preparation_status(self, chapter_ids):
        return self.repository.preparation_status(self.book_id, chapter_ids)

    def rebuild_graph(self):
        return self.repository.rebuild_graph(self.book_id)

    def get_scenes_for_entity(self, book_entity_id):
        return self.repository.scenes_for_entity(self.book_id, book_entity_id)

    def get_entities_for_scene(self, scene_id):
        return self.repository.entities_for_scene(self.book_id, scene_id)

    def get_next_scene(self, scene_id):
        return self.repository.adjacent_scene(self.book_id, scene_id)

    def get_previous_scene(self, scene_id):
        return self.repository.adjacent_scene(
            self.book_id, scene_id, previous=True,
        )

    def mentions(self, scene_id=None, *, unresolved_only=False):
        return self.repository.mentions(self.book_id, scene_id,
                                        unresolved_only=unresolved_only)

    def references(self, scene_id=None):
        return self.repository.references(self.book_id, scene_id)

    def entities(self):
        return self.repository.entities(self.book_id)

    def link_mention(self, mention_id, entity_id):
        self.repository.link(self.book_id, mention_id, entity_id)

    def unlink_mention(self, mention_id):
        self.repository.unlink(self.book_id, mention_id)
