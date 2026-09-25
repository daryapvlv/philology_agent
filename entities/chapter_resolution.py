"""Candidate generation и model-based resolution внутри одной главы."""
from difflib import SequenceMatcher
from copy import deepcopy
from dataclasses import replace
import json
import re
from uuid import NAMESPACE_URL, uuid5

from infrastructure.files import json_sha256
from llm.requests import request_json

from .chapter_prompt import CHAPTER_BATCH_RESOLUTION_PROMPT, CHAPTER_RECONCILIATION_PROMPT
from .config import ChapterResolutionConfig


_RU_ENDINGS = tuple(sorted({
    "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими", "иной",
    "иной", "ая", "яя", "ой", "ей", "ам", "ям", "ах", "ях", "ом", "ем",
    "ов", "ев", "ин", "ын", "ы", "и", "а", "я", "у", "ю", "е",
}, key=len, reverse=True))


def _tokens(value):
    return re.findall(r"[\wё]+", (value or "").casefold(), re.UNICODE)


def _stems(value):
    result = []
    for token in _tokens(value):
        stem = token
        for ending in _RU_ENDINGS:
            if len(token) - len(ending) >= 4 and token.endswith(ending):
                stem = token[:-len(ending)]
                break
        result.append(stem)
    return result


def _type_conflict(left_type, right_type):
    """Person и vehicle/location/object — никогда не один referent, даже при
    сильном лексическом совпадении. Не блокирует пары, где тип с одной из
    сторон ещё не определён (None) — там решение по-прежнему за LLM."""
    return bool(left_type and right_type and left_type != right_type)


def _similarity(mention, entity, scene_rank):
    if _type_conflict(mention.get("entity_type"), entity.get("entity_type")):
        return 0.0
    values = [entity["canonical_name"], *entity.get("aliases", []),
              *entity.get("canonical_hints", []),
              *entity.get("representative_references", [])]
    source = mention.get("canonical_name_hint") or mention["surface_text"]
    lexical = max(SequenceMatcher(None, source.casefold(), value.casefold()).ratio()
                  for value in values)
    source_stems = set(_stems(source))
    overlap = max((len(source_stems & set(_stems(value))) /
                   max(1, len(source_stems | set(_stems(value)))) for value in values),
                  default=0)
    type_score = 0.08 if (mention.get("entity_type") and entity.get("entity_type")
                          and mention["entity_type"] == entity["entity_type"]) else 0
    last_scene = max(entity.get("scene_ranks", [-1000]))
    proximity = 0.04 / (1 + abs(scene_rank - last_scene))
    return max(lexical, overlap) + type_score + proximity


def generate_candidates(mention, entities, *, scene_rank, top_k):
    """Сигналы только ранжируют кандидатов и никогда не принимают merge-решение.
    Кандидаты с конфликтующим entity_type исключаются полностью, а не просто
    понижаются в ранге — иначе при малом числе сущностей категорически
    несовместимый кандидат (человек и транспорт) всё равно мог бы попасть
    в top_k и быть предложен LLM как вариант."""
    if type(top_k) is not int or not 1 <= top_k <= 20:
        raise ValueError("top_k должен быть от 1 до 20")
    eligible = [entity for entity in entities
               if not _type_conflict(mention.get("entity_type"), entity.get("entity_type"))]
    ranked = sorted(eligible,
                    key=lambda entity: (-_similarity(mention, entity, scene_rank),
                                        entity["id"]))
    return ranked[:top_k]


def _entity_similarity(left, right):
    if _type_conflict(left.get("entity_type"), right.get("entity_type")):
        return 0.0
    scores = []
    for left_value in [left["canonical_name"], *left.get("aliases", []),
                       *left.get("canonical_hints", []),
                       *left.get("representative_references", [])]:
        for right_value in [right["canonical_name"], *right.get("aliases", []),
                            *right.get("canonical_hints", []),
                            *right.get("representative_references", [])]:
            lexical = SequenceMatcher(
                None, left_value.casefold(), right_value.casefold(),
            ).ratio()
            left_stems, right_stems = set(_stems(left_value)), set(_stems(right_value))
            overlap = len(left_stems & right_stems) / max(1, len(left_stems | right_stems))
            scores.append(max(lexical, overlap))
    type_score = 0.08 if (left.get("entity_type") and
                          left.get("entity_type") == right.get("entity_type")) else 0
    return max(scores, default=0) + type_score


def reconciliation_pairs(entities, top_k):
    """Эвристики формируют shortlist, но не определяют итоговый merge."""
    pairs = {}
    for source in entities:
        candidates = sorted(
            (candidate for candidate in entities if candidate["id"] != source["id"]
             and not _type_conflict(source.get("entity_type"), candidate.get("entity_type"))),
            key=lambda candidate: (-_entity_similarity(source, candidate), candidate["id"]),
        )[:top_k]
        for candidate in candidates:
            key = tuple(sorted((source["id"], candidate["id"])))
            pairs[key] = (source, candidate)
    return [pairs[key] for key in sorted(pairs)]


def _context(text, start, end, limit):
    side = max(0, (limit - (end - start)) // 2)
    left, right = max(0, start - side), min(len(text), end + side)
    return text[left:right]


def _cluster_id(book_id, chapter_id, mention_id):
    return "chapter-entity:" + uuid5(
        NAMESPACE_URL, f"{book_id}:{chapter_id}:{mention_id}"
    ).hex


def _batch_resolutions(content, mentions, candidate_map):
    expected = {mention["id"] for mention in mentions}
    assigned, parsed = set(), []
    rows = content.get("resolutions") if isinstance(content, dict) else None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("mention_ids"), list):
                continue
            ids = row["mention_ids"]
            if (not ids or any(not isinstance(item, str) for item in ids)
                    or len(ids) != len(set(ids)) or not set(ids) <= expected
                    or set(ids) & assigned):
                continue
            choices = sum((row.get("entity_id") is not None,
                           row.get("new_entity") is not None,
                           row.get("unresolved") is True))
            if choices != 1:
                continue
            if row.get("entity_id") is not None:
                if any(row["entity_id"] not in candidate_map[mention_id]
                       for mention_id in ids):
                    continue
                kind = "existing"
            elif row.get("new_entity") is not None:
                if not isinstance(row["new_entity"], dict):
                    continue
                kind = "new"
            else:
                kind = "unresolved"
            assigned.update(ids)
            parsed.append({"mention_ids": ids, "kind": kind,
                           "entity_id": row.get("entity_id"),
                           "new_entity": row.get("new_entity")})
    parsed.extend({"mention_ids": [mention["id"]], "kind": "unresolved",
                   "entity_id": None, "new_entity": None}
                  for mention in mentions if mention["id"] not in assigned)
    return parsed


def _representatives(entity, limit):
    evidence = entity.pop("_evidence")
    mentions = sorted(evidence, key=lambda item: (-len(item["surface_text"]),
                                                  item["mention_id"]))
    entity["representative_mentions"] = list(dict.fromkeys(
        item["surface_text"] for item in mentions[:limit]
    ))
    contexts = [evidence[0]["context"]]
    contexts.extend(item["context"] for item in evidence[-limit:] if item["context"] not in contexts)
    entity["representative_contexts"] = contexts[:limit]


def _public_entity(entity):
    return {key: entity.get(key) for key in (
        "id", "canonical_name", "entity_type", "aliases", "representative_mentions",
        "representative_contexts", "representative_references", "canonical_hints",
        "scene_ids",
    )}


def _canonical_name(values):
    values = list(dict.fromkeys(value.strip() for value in values
                                if isinstance(value, str) and value.strip()))
    # Среди форм с равным числом токенов короче обычно значит именительный
    # падеж: русские косвенные окончания (-ой, -ей, -ому...) почти всегда
    # длиннее именительных (-а, -я, пустое). Предпочитать длинную форму на
    # равенстве — систематически выбирать не тот падеж.
    return sorted(values, key=lambda value: (-len(_tokens(value)), len(value), value))[0]


def _grounded_canonical(requested, mentions):
    if not isinstance(requested, str) or not requested.strip():
        return None
    requested = requested.strip()
    evidence = [value for mention in mentions for value in (
        mention.get("surface_text"), mention.get("canonical_name_hint"),
    ) if isinstance(value, str) and value.strip()]
    requested_stems = set(_stems(requested))
    evidence_stems = {stem for value in evidence for stem in _stems(value)}
    if requested_stems and requested_stems <= evidence_stems:
        return requested
    # Снятие одного суффикса не всегда даёт общий стем для формы именительного
    # и косвенного падежа одной русской фамилии (например "Негулина" и
    # "Негулиной" расходятся сильнее одного окончания у фамилий на -ин/-ов).
    # Предложенное имя, почти буквально совпадающее с реальным mention, —
    # падежный вариант того же имени, а не выдуманное моделью, даже когда
    # грубый стемминг это не видит.
    if requested_stems and any(
        SequenceMatcher(None, requested.casefold(), value.casefold()).ratio() >= 0.82
        for value in evidence
    ):
        return requested
    return None


def _reconciliation_decisions(content, expected_pairs):
    valid = {"SAME_ENTITY", "DIFFERENT_ENTITY", "UNCERTAIN"}
    expected = {tuple(sorted(pair)) for pair in expected_pairs}
    parsed = {}
    rows = content.get("decisions") if isinstance(content, dict) else None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            left, right = row.get("left_entity_id"), row.get("right_entity_id")
            if not isinstance(left, str) or not isinstance(right, str) or left == right:
                continue
            pair = tuple(sorted((left, right)))
            decision = row.get("decision")
            if pair in expected and decision in valid and pair not in parsed:
                parsed[pair] = decision
    return [{"left_entity_id": pair[0], "right_entity_id": pair[1],
             "decision": parsed.get(pair, "UNCERTAIN")}
            for pair in sorted(expected)]


def _batches(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _conservative_unions(entity_ids, decisions):
    verdicts = {
        tuple(sorted((row["left_entity_id"], row["right_entity_id"]))): row["decision"]
        for row in decisions
    }
    groups = [{entity_id} for entity_id in entity_ids]
    unions = []
    for pair in sorted(key for key, value in verdicts.items() if value == "SAME_ENTITY"):
        left_group = next(group for group in groups if pair[0] in group)
        right_group = next(group for group in groups if pair[1] in group)
        if left_group is right_group:
            continue
        cross_pairs = [tuple(sorted((left, right)))
                       for left in left_group for right in right_group]
        if all(verdicts.get(cross_pair) == "SAME_ENTITY" for cross_pair in cross_pairs):
            unions.append(pair)
            left_group.update(right_group)
            groups.remove(right_group)
    return unions


def _merge_entities(entities, unions, representative_limit):
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

    merged, remap = [], {}
    for root_id, group in groups.items():
        mention_forms = list(dict.fromkeys(
            form for entity in group for form in entity["representative_mentions"]
        ))
        aliases = list(dict.fromkeys(
            alias for entity in group for alias in entity["aliases"]
        ))
        hints = list(dict.fromkeys(
            hint for entity in group for hint in entity.get("canonical_hints", [])
        ))
        mention_ids = list(dict.fromkeys(
            mention_id for entity in group for mention_id in entity["mention_ids"]
        ))
        scene_ids = list(dict.fromkeys(
            scene_id for entity in group for scene_id in entity["scene_ids"]
        ))
        contexts = list(dict.fromkeys(
            context for entity in group for context in entity["representative_contexts"]
        ))[:representative_limit]
        references = list(dict.fromkeys(
            reference for entity in group
            for reference in entity.get("representative_references", [])
        ))[:representative_limit]
        canonical_candidates = [
            *(entity["canonical_name"] for entity in group), *hints,
        ]
        canonical_name = _canonical_name(canonical_candidates or mention_forms)
        types = [entity["entity_type"] for entity in group if entity.get("entity_type")]
        merged_entity = {
            "id": root_id, "book_id": group[0]["book_id"],
            "chapter_id": group[0]["chapter_id"], "canonical_name": canonical_name,
            "entity_type": types[0] if types and len(set(types)) == 1 else None,
            "aliases": aliases, "mention_ids": mention_ids, "scene_ids": scene_ids,
            "canonical_hints": hints,
            "representative_mentions": mention_forms[:representative_limit],
            "representative_contexts": contexts,
            "representative_references": references,
            "resolution_state": "reconciled" if len(group) > 1 else group[0]["resolution_state"],
        }
        merged.append(merged_entity)
        remap.update({entity["id"]: root_id for entity in group})
    return merged, remap


class ChapterEntityResolver:
    def __init__(self, client, *, config=None,
                 cache_dir=".cache/entities/chapter-resolution"):
        self.client = client
        self.config = (ChapterResolutionConfig.from_config(config)
                       if config is not None else ChapterResolutionConfig())
        if not 1 <= self.config.representative_contexts <= 10:
            raise ValueError("representative_contexts должен быть от 1 до 10")
        if not 1 <= self.config.resolution_batch_size <= 30:
            raise ValueError("resolution_batch_size должен быть от 1 до 30")
        if not 1 <= self.config.reconciliation_batch_size <= 20:
            raise ValueError("reconciliation_batch_size должен быть от 1 до 20")
        self.cache_dir = cache_dir

    def _reconcile(self, entities):
        usage = {"calls": 0, "tokens": 0, "cache_hits": 0}
        decisions = []
        cache_dir = (f"{self.cache_dir}/reconciliation"
                     if self.cache_dir is not None else None)
        pairs = reconciliation_pairs(entities, self.config.top_k)
        batches = list(_batches(pairs, self.config.reconciliation_batch_size))
        call_config = replace(self.config, max_calls=max(self.config.max_calls, len(batches)))
        for batch in batches:
            unique = {entity["id"]: entity for pair in batch for entity in pair}
            pair_ids = [(left["id"], right["id"]) for left, right in batch]
            payload = json.dumps({
                "chapter_entities": [_public_entity(unique[key]) for key in sorted(unique)],
                "candidate_pairs": [
                    {"left_entity_id": left, "right_entity_id": right}
                    for left, right in pair_ids
                ],
            }, ensure_ascii=False)
            raw = request_json(self.client, call_config, CHAPTER_RECONCILIATION_PROMPT,
                               payload, cache_dir, usage)
            if raw is None:
                raise ValueError("Бюджет chapter entity reconciliation исчерпан")
            if raw["finish_reason"] != "stop":
                content = None
            else:
                try:
                    content = json.loads(raw["content"])
                except (TypeError, json.JSONDecodeError):
                    content = None
            decisions.extend(_reconciliation_decisions(content, pair_ids))
        unions = _conservative_unions(
            [entity["id"] for entity in entities], decisions,
        )
        final, remap = _merge_entities(
            entities, unions, self.config.representative_contexts,
        )
        return final, remap, decisions, usage

    def resolve(self, *, book_id, chapter_id, scenes, mentions, references=()):
        scene_by_id = {scene["id"]: scene for scene in scenes}
        scene_rank = {scene["id"]: index for index, scene in enumerate(scenes)}
        ordered = sorted(mentions, key=lambda item: (
            scene_rank[item["scene_id"]], item["start_offset"], item["end_offset"], item["id"]
        ))
        evidence = {}
        for mention in ordered:
            scene = scene_by_id[mention["scene_id"]]
            evidence[mention["id"]] = _context(
                scene["text"], mention["start_offset"], mention["end_offset"],
                self.config.context_chars,
            )
        entities, usage = [], {"calls": 0, "tokens": 0, "cache_hits": 0}
        decisions = []
        batches = list(_batches(ordered, self.config.resolution_batch_size))
        call_config = replace(self.config, max_calls=max(self.config.max_calls, len(batches)))
        for batch in batches:
            candidate_map, candidates = {}, {}
            for mention in batch:
                ranked = generate_candidates(
                    mention, entities, scene_rank=scene_rank[mention["scene_id"]],
                    top_k=self.config.top_k,
                )
                candidate_map[mention["id"]] = [entity["id"] for entity in ranked]
                candidates.update({entity["id"]: entity for entity in ranked})
            mention_rows = []
            for mention in batch:
                row = {key: mention.get(key) for key in (
                    "id", "surface_text", "entity_type", "canonical_name_hint",
                    "scene_id", "start_offset", "end_offset",
                )}
                row["source_context"] = evidence[mention["id"]]
                row["candidate_entity_ids"] = candidate_map[mention["id"]]
                mention_rows.append(row)
            payload = json.dumps({
                "mentions": mention_rows,
                "scenes": [{"id": scene_id, "order": scene_rank[scene_id]}
                           for scene_id in dict.fromkeys(m["scene_id"] for m in batch)],
                "existing_chapter_entities": [
                    _public_entity(candidates[key]) for key in sorted(candidates)
                ],
            }, ensure_ascii=False)
            raw = request_json(self.client, call_config, CHAPTER_BATCH_RESOLUTION_PROMPT,
                               payload, self.cache_dir, usage)
            if raw is None:
                raise ValueError("Бюджет batch chapter entity resolution исчерпан")
            if raw["finish_reason"] != "stop":
                content = None
            else:
                try:
                    content = json.loads(raw["content"])
                except (TypeError, json.JSONDecodeError):
                    content = None
            by_id = {mention["id"]: mention for mention in batch}
            groups = _batch_resolutions(content, batch, candidate_map)
            for group in groups:
                group_mentions = [by_id[mention_id] for mention_id in group["mention_ids"]]
                if group["kind"] == "unresolved":
                    subgroups = [[mention] for mention in group_mentions]
                else:
                    subgroups = [group_mentions]
                for subgroup in subgroups:
                    if group["kind"] == "existing":
                        target = next(entity for entity in entities
                                      if entity["id"] == group["entity_id"])
                    else:
                        forms = list(dict.fromkeys(m["surface_text"] for m in subgroup))
                        requested = ((group.get("new_entity") or {}).get("canonical_name")
                                     if group["kind"] == "new" else None)
                        canonical = (_grounded_canonical(requested, subgroup)
                                     or _canonical_name([
                                         *(m.get("canonical_name_hint") for m in subgroup),
                                         *forms,
                                     ]))
                        types = [m.get("entity_type") for m in subgroup if m.get("entity_type")]
                        target = {
                            "id": _cluster_id(book_id, chapter_id, subgroup[0]["id"]),
                            "book_id": book_id, "chapter_id": chapter_id,
                            "canonical_name": canonical,
                            "entity_type": types[0] if types and len(set(types)) == 1 else None,
                            "aliases": [], "mention_ids": [], "scene_ids": [],
                            "canonical_hints": [],
                            "scene_ranks": [], "representative_mentions": [],
                            "representative_contexts": [],
                            "representative_references": [], "_evidence": [],
                            "resolution_state": group["kind"],
                        }
                        entities.append(target)
                    for mention in subgroup:
                        target["mention_ids"].append(mention["id"])
                        target["aliases"] = list(dict.fromkeys([
                            *target["aliases"], mention["surface_text"],
                        ]))
                        if mention.get("canonical_name_hint"):
                            target["canonical_hints"] = list(dict.fromkeys([
                                *target.get("canonical_hints", []),
                                mention["canonical_name_hint"],
                            ]))
                        target["scene_ids"] = list(dict.fromkeys([
                            *target["scene_ids"], mention["scene_id"],
                        ]))
                        target["scene_ranks"] = list(dict.fromkeys([
                            *target["scene_ranks"], scene_rank[mention["scene_id"]],
                        ]))
                        target["_evidence"].append({
                            "mention_id": mention["id"],
                            "surface_text": mention["surface_text"],
                            "context": evidence[mention["id"]],
                        })
                        decisions.append({"mention_id": mention["id"],
                                          "resolution": group["kind"],
                                          "entity_id": target["id"]})
                    preview = dict(target)
                    _representatives(preview, self.config.representative_contexts)
                    target.update({key: preview[key] for key in
                                   ("representative_mentions", "representative_contexts")})
        references_by_mention = {}
        for reference in references:
            target_id = reference.get("target_mention_id")
            scene = scene_by_id.get(reference.get("scene_id"))
            if target_id and scene:
                references_by_mention.setdefault(target_id, []).append({
                    "surface_text": reference.get("surface_text"),
                    "context": _context(
                        scene["text"], reference.get("start_offset", 0),
                        reference.get("end_offset", 0), self.config.context_chars,
                    ),
                })
        for entity in entities:
            reference_rows = [row for mention_id in entity["mention_ids"]
                              for row in references_by_mention.get(mention_id, [])]
            entity["representative_references"] = list(dict.fromkeys(
                row["surface_text"] for row in reference_rows
                if isinstance(row.get("surface_text"), str)
            ))[:self.config.representative_contexts]
            _representatives(entity, self.config.representative_contexts)
            entity["representative_contexts"] = list(dict.fromkeys([
                *entity["representative_contexts"],
                *(row["context"] for row in reference_rows),
            ]))[:self.config.representative_contexts]
            entity.pop("scene_ranks", None)
        provisional_entities = deepcopy(entities)
        entities, remap, reconciliation_decisions, reconciliation_usage = self._reconcile(
            entities,
        )
        for decision in decisions:
            decision["provisional_entity_id"] = decision["entity_id"]
            decision["entity_id"] = remap[decision["entity_id"]]
        usage = {key: usage[key] + reconciliation_usage[key] for key in usage}
        input_hash = json_sha256({
            "chapter_id": chapter_id,
            "mentions": [(m["id"], m.get("scene_text_hash")) for m in ordered],
            "config": {"top_k": self.config.top_k,
                       "resolution_batch_size": self.config.resolution_batch_size,
                       "context_chars": self.config.context_chars,
                       "reconciliation_batch_size": self.config.reconciliation_batch_size,
                       "representative_contexts": self.config.representative_contexts,
                       "model": self.config.model},
        })
        return {"entities": entities, "provisional_entities": provisional_entities,
                "decisions": decisions,
                "reconciliation_decisions": reconciliation_decisions,
                "input_hash": input_hash, "usage": usage}
