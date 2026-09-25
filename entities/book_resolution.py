"""Batch resolution ChapterEntity -> BookEntity внутри одной книги."""
from dataclasses import replace
from difflib import SequenceMatcher
import json
from uuid import NAMESPACE_URL, uuid5

from llm.requests import request_json
from llm.tokens import TokenCounter

from .chapter_resolution import (
    _batches, _conservative_unions, _reconciliation_decisions, _stems, _type_conflict,
)
from .config import BookResolutionConfig
from .prompt import BOOK_RECONCILIATION_PROMPT, BOOK_RESOLUTION_PROMPT


def _book_entity_id(book_id, chapter_entity_id):
    return "book-entity:" + uuid5(
        NAMESPACE_URL, f"{book_id}:{chapter_entity_id}",
    ).hex


def _values(entity):
    return [value for value in dict.fromkeys([
        entity.get("canonical_name", ""), *entity.get("aliases", []),
        *entity.get("representative_mentions", []),
        *entity.get("canonical_hints", []),
        *entity.get("representative_references", []),
    ]) if isinstance(value, str) and value]


def _canonical_name(values):
    values = list(dict.fromkeys(value.strip() for value in values
                                if isinstance(value, str) and value.strip()))
    # См. chapter_resolution._canonical_name: на равном числе стемов короче
    # обычно значит именительный падеж — косвенные русские окончания почти
    # всегда длиннее именительных.
    return sorted(values, key=lambda value: (-len(_stems(value)), len(value), value))[0]


def _similarity(source, candidate):
    if _type_conflict(source.get("entity_type"), candidate.get("entity_type")):
        return 0.0
    scores = []
    for left in _values(source):
        for right in _values(candidate):
            lexical = SequenceMatcher(None, left.casefold(), right.casefold()).ratio()
            left_stems, right_stems = set(_stems(left)), set(_stems(right))
            overlap = len(left_stems & right_stems) / max(1, len(left_stems | right_stems))
            scores.append(max(lexical, overlap))
    type_score = 0.08 if (source.get("entity_type") and
                          source.get("entity_type") == candidate.get("entity_type")) else 0
    return max(scores, default=0) + type_score


def book_candidates(source, entities, top_k):
    eligible = [entity for entity in entities
               if not _type_conflict(source.get("entity_type"), entity.get("entity_type"))]
    return sorted(eligible, key=lambda item: (-_similarity(source, item), item["id"]))[:top_k]


def book_reconciliation_pairs(entities, top_k):
    """Shortlist уже резолвленных BookEntity для повторной сверки — тот же
    эвристический паттерн, что и `book_candidates`/chapter-level
    `reconciliation_pairs`, финальное решение остаётся за LLM. Нужен, потому
    что `resolve()` обрабатывает ChapterEntity по одной и решает сразу: если
    контекста для diminutive (`Петруша`) в моменте решения не хватило, шанса
    пересмотреть его при появлении остальных упоминаний персонажа больше нет
    без этого отдельного прохода.

    Возвращает (left, right, score) — score сохраняется вызывающим кодом как
    эвристическая оценка UNCERTAIN-пар (POSSIBLY_SAME.score), а не только для
    ранжирования: пересчитывать его отдельно после LLM-решения незачем."""
    pairs = {}
    for source in entities:
        candidates = sorted(
            (candidate for candidate in entities if candidate["id"] != source["id"]
             and not _type_conflict(source.get("entity_type"), candidate.get("entity_type"))),
            key=lambda candidate: (-_similarity(source, candidate), candidate["id"]),
        )[:top_k]
        for candidate in candidates:
            key = tuple(sorted((source["id"], candidate["id"])))
            score = _similarity(source, candidate)
            if key not in pairs or score > pairs[key][2]:
                pairs[key] = (source, candidate, score)
    return [pairs[key] for key in sorted(pairs)]


def _public_book_entity(entity):
    return {key: entity.get(key) for key in (
        "id", "canonical_name", "entity_type", "aliases", "canonical_hints",
        "representative_contexts", "representative_references",
        "chapter_ids", "scene_ids",
    )}


def _merge_book_entities(entities, unions, representative_limit):
    by_id = {entity["id"]: entity for entity in entities}
    parent = {entity_id: entity_id for entity_id in by_id}

    def root(entity_id):
        while parent[entity_id] != entity_id:
            parent[entity_id] = parent[parent[entity_id]]
            entity_id = parent[entity_id]
        return entity_id

    for left_id, right_id in unions:
        left_root, right_root = root(left_id), root(right_id)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    groups = {}
    for entity in entities:
        groups.setdefault(root(entity["id"]), []).append(entity)

    merges = []
    for group in groups.values():
        if len(group) == 1:
            continue
        survivor = min(group, key=lambda entity: entity["id"])
        aliases = list(dict.fromkeys(
            alias for entity in group for alias in entity.get("aliases", [])
        ))
        hints = list(dict.fromkeys(
            hint for entity in group for hint in entity.get("canonical_hints", [])
        ))
        chapter_ids = list(dict.fromkeys(
            chapter_id for entity in group for chapter_id in entity.get("chapter_ids", [])
        ))
        scene_ids = list(dict.fromkeys(
            scene_id for entity in group for scene_id in entity.get("scene_ids", [])
        ))
        contexts = list(dict.fromkeys(
            context for entity in group for context in entity.get("representative_contexts", [])
        ))[:representative_limit]
        references = list(dict.fromkeys(
            reference for entity in group
            for reference in entity.get("representative_references", [])
        ))[:representative_limit]
        canonical_name = _canonical_name([
            *(entity["canonical_name"] for entity in group), *hints,
        ])
        merged = {
            **survivor, "canonical_name": canonical_name, "aliases": aliases,
            "canonical_hints": hints, "chapter_ids": chapter_ids, "scene_ids": scene_ids,
            "representative_contexts": contexts, "representative_references": references,
        }
        losers = [entity["id"] for entity in group if entity["id"] != survivor["id"]]
        merges.append({"survivor_id": survivor["id"], "loser_ids": losers, "entity": merged})
    return merges


def _parse(content, batch, candidate_map):
    expected = {item["id"] for item in batch}
    rows = content.get("resolutions") if isinstance(content, dict) else None
    parsed, seen, duplicated = {}, set(), set()
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            source_id, decision = row.get("chapter_entity_id"), row.get("decision")
            if source_id not in expected:
                continue
            if source_id in seen:
                duplicated.add(source_id)
                parsed.pop(source_id, None)
                continue
            valid = decision in {"same_entity", "new_entity", "uncertain"}
            if decision == "same_entity":
                valid = valid and row.get("entity_id") in candidate_map[source_id]
            if not valid:
                continue
            seen.add(source_id)
            if source_id not in duplicated:
                parsed[source_id] = {"chapter_entity_id": source_id,
                                     "decision": decision,
                                     "entity_id": row.get("entity_id")}
    return [parsed.get(item["id"], {"chapter_entity_id": item["id"],
                                     "decision": "uncertain", "entity_id": None})
            for item in batch]


def _new_entity(book_id, source):
    # representative_mentions — сырые формы из текста (падежные и все прочие),
    # они уже учтены в canonical_name на уровне главы (см.
    # CHAPTER_BATCH_RESOLUTION_PROMPT: для person/location это именительный
    # падеж). Подмешивать их сюда как отдельных кандидатов — значит заново
    # позволить более длинной падежной форме перебить уже верно выбранное имя.
    canonical = _canonical_name([
        source.get("canonical_name"), *source.get("canonical_hints", []),
    ])
    return {
        "id": _book_entity_id(book_id, source["id"]), "book_id": book_id,
        "canonical_name": canonical, "entity_type": source.get("entity_type"),
        "aliases": list(dict.fromkeys(source.get("aliases", []))),
        "canonical_hints": list(dict.fromkeys(source.get("canonical_hints", []))),
        "chapter_ids": [source["chapter_id"]],
        "scene_ids": list(dict.fromkeys(source.get("scene_ids", []))),
        "representative_contexts": list(source.get("representative_contexts", [])),
        "representative_references": list(source.get(
            "representative_references", []
        )),
    }


def _extend(entity, source, context_limit):
    entity["canonical_name"] = _canonical_name([
        entity.get("canonical_name"), source.get("canonical_name"),
        *entity.get("canonical_hints", []), *source.get("canonical_hints", []),
    ])
    entity["aliases"] = list(dict.fromkeys([
        *entity.get("aliases", []), *source.get("aliases", []),
    ]))
    entity["canonical_hints"] = list(dict.fromkeys([
        *entity.get("canonical_hints", []), *source.get("canonical_hints", []),
    ]))
    entity["chapter_ids"] = list(dict.fromkeys([
        *entity.get("chapter_ids", []), source["chapter_id"],
    ]))
    entity["scene_ids"] = list(dict.fromkeys([
        *entity.get("scene_ids", []), *source.get("scene_ids", []),
    ]))
    entity["representative_contexts"] = list(dict.fromkeys([
        *entity.get("representative_contexts", []),
        *source.get("representative_contexts", []),
    ]))[:context_limit]
    entity["representative_references"] = list(dict.fromkeys([
        *entity.get("representative_references", []),
        *source.get("representative_references", []),
    ]))[:context_limit]


def _payload(batch, entities, *, top_k, context_limit):
    candidate_map, payload_rows = {}, []
    for source in batch:
        candidates = book_candidates(source, list(entities.values()), top_k)
        candidate_map[source["id"]] = [item["id"] for item in candidates]
        payload_rows.append({
            **{key: source.get(key) for key in (
                "id", "canonical_name", "entity_type", "aliases",
                "canonical_hints", "representative_references",
                "chapter_id", "scene_ids",
            )},
            "chapter_entity_id": source["id"],
            "representative_contexts": source.get("representative_contexts", [])[
                :context_limit
            ],
            "candidates": [{
                **{key: candidate.get(key) for key in (
                    "id", "canonical_name", "entity_type", "aliases",
                    "canonical_hints", "representative_references",
                    "chapter_ids", "scene_ids",
                )},
                "entity_id": candidate["id"],
                "representative_contexts": candidate.get(
                    "representative_contexts", []
                )[:context_limit],
            } for candidate in candidates],
        })
    return (json.dumps({"chapter_entities": payload_rows}, ensure_ascii=False),
            candidate_map)


def _fits(prompt, payload, config, counter):
    if len(prompt) + len(payload) > config.max_input_chars:
        return False
    messages = [{"role": "system", "content": prompt},
                {"role": "user", "content": payload}]
    return counter.request(messages, response_format={"type": "json_object"}) \
        <= config.max_input_tokens


class BookEntityResolver:
    def __init__(self, client, *, config=None,
                 cache_dir=".cache/entities/book-resolution"):
        self.client = client
        self.config = (BookResolutionConfig.from_config(config)
                       if config is not None else BookResolutionConfig())
        if not 1 <= self.config.top_k <= 20 or not 1 <= self.config.batch_size <= 30:
            raise ValueError("Некорректные top_k или batch_size book resolution")
        if not 1 <= self.config.reconciliation_batch_size <= 20:
            raise ValueError("Некорректный reconciliation_batch_size book resolution")
        if not 1 <= self.config.representative_contexts <= 10:
            raise ValueError("representative_contexts должен быть от 1 до 10")
        self.cache_dir = cache_dir

    def resolve(self, *, book_id, chapter_entities, book_entities, apply_batch=None):
        entities = {item["id"]: dict(item) for item in book_entities
                    if item.get("book_id") == book_id}
        chapter_entities = [item for item in chapter_entities
                            if item.get("book_id") == book_id]
        usage = {"calls": 0, "tokens": 0, "cache_hits": 0}
        decisions = []
        counter = TokenCounter(self.config.model)
        # Resolution идёт строго по одной ChapterEntity: общий max_calls=64
        # обрывал большие книги. Бюджет вызовов и токенов должен покрывать
        # фактический объём книги, как в chapter-level resolver.
        call_config = replace(
            self.config,
            max_calls=max(self.config.max_calls, len(chapter_entities)),
            max_total_tokens=max(
                self.config.max_total_tokens,
                len(chapter_entities) * self.config.max_output_tokens,
            ),
        )
        position = 0
        while position < len(chapter_entities):
            # В отличие от независимой классификации, результат текущей строки
            # меняет множество кандидатов для следующей. Поэтому один LLM batch
            # здесь семантически некорректен: две новые сущности одного batch не
            # могли увидеть друг друга и навсегда становились дублями.
            size = 1
            context_limit, top_k = self.config.representative_contexts, self.config.top_k
            while True:
                batch = chapter_entities[position:position + size]
                payload, candidate_map = _payload(
                    batch, entities, top_k=top_k, context_limit=context_limit,
                )
                if _fits(BOOK_RESOLUTION_PROMPT, payload, self.config, counter):
                    break
                if size > 1:
                    size = max(1, size // 2)
                elif context_limit > 1:
                    context_limit -= 1
                elif top_k > 1:
                    top_k -= 1
                else:
                    raise ValueError(
                        "Одна ChapterEntity не помещается в лимит book resolution; "
                        "увеличь max_input_chars/max_input_tokens"
                    )
            raw = request_json(self.client, call_config, BOOK_RESOLUTION_PROMPT,
                               payload,
                               self.cache_dir, usage)
            if raw is None:
                raise ValueError("Бюджет book entity resolution исчерпан")
            try:
                content = json.loads(raw["content"]) if raw["finish_reason"] == "stop" else None
            except (TypeError, json.JSONDecodeError):
                content = None
            batch_decisions = _parse(content, batch, candidate_map)
            changed = {}
            source_by_id = {item["id"]: item for item in batch}
            for decision in batch_decisions:
                source = source_by_id[decision["chapter_entity_id"]]
                if decision["decision"] == "new_entity":
                    entity = _new_entity(book_id, source)
                    entities[entity["id"]] = entity
                    changed[entity["id"]] = entity
                    decision["entity_id"] = entity["id"]
                elif decision["decision"] == "same_entity":
                    entity = entities[decision["entity_id"]]
                    _extend(entity, source, self.config.representative_contexts)
                    changed[entity["id"]] = entity
            if apply_batch:
                apply_batch(batch, batch_decisions, list(changed.values()))
            decisions.extend(batch_decisions)
            position += len(batch)
        return {"entities": list(entities.values()), "decisions": decisions, "usage": usage}

    def reconcile(self, *, book_id, entities):
        """Повторно сверить уже резолвленные BookEntity одной книги.

        `resolve()` решает по одной ChapterEntity за раз и никогда не
        возвращается к прошлым решениям, поэтому diminutive или неполное
        упоминание, для которого в моменте резолюции не набралось evidence
        (`Петруша` при уже существующем `Петр Андреевич`), остаётся
        отдельной сущностью навсегда. Этот проход даёт LLM обе стороны
        сразу, с накопленными за все главы representative_contexts — тот же
        принцип, что у chapter-level `_reconcile`, только над BookEntity.
        """
        entities = {item["id"]: dict(item) for item in entities
                    if item.get("book_id") == book_id}
        usage = {"calls": 0, "tokens": 0, "cache_hits": 0}
        pairs = book_reconciliation_pairs(list(entities.values()), self.config.top_k)
        if not pairs:
            return {"entities": list(entities.values()), "merges": [],
                    "decisions": [], "usage": usage}
        scores = {tuple(sorted((left["id"], right["id"]))): score for left, right, score in pairs}
        cache_dir = f"{self.cache_dir}/reconciliation" if self.cache_dir is not None else None
        decisions = []
        batches = list(_batches(pairs, self.config.reconciliation_batch_size))
        call_config = replace(
            self.config,
            max_calls=max(self.config.max_calls, len(batches)),
            max_total_tokens=max(
                self.config.max_total_tokens,
                len(batches) * self.config.max_output_tokens,
            ),
        )
        for batch in batches:
            unique = {entity["id"]: entity for left, right, _score in batch for entity in (left, right)}
            pair_ids = [(left["id"], right["id"]) for left, right, _score in batch]
            payload = json.dumps({
                "book_entities": [_public_book_entity(unique[key]) for key in sorted(unique)],
                "candidate_pairs": [
                    {"left_entity_id": left, "right_entity_id": right}
                    for left, right in pair_ids
                ],
            }, ensure_ascii=False)
            raw = request_json(self.client, call_config, BOOK_RECONCILIATION_PROMPT,
                               payload, cache_dir, usage)
            if raw is None:
                raise ValueError("Бюджет book entity reconciliation исчерпан")
            try:
                content = json.loads(raw["content"]) if raw["finish_reason"] == "stop" else None
            except (TypeError, json.JSONDecodeError):
                content = None
            decisions.extend(_reconciliation_decisions(content, pair_ids))
        for decision in decisions:
            key = tuple(sorted((decision["left_entity_id"], decision["right_entity_id"])))
            decision["score"] = scores.get(key)
        unions = _conservative_unions(list(entities.keys()), decisions)
        merges = _merge_book_entities(
            list(entities.values()), unions, self.config.representative_contexts,
        )
        return {"entities": list(entities.values()), "merges": merges,
                "decisions": decisions, "usage": usage}
