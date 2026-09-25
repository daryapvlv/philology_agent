"""Чистая проверка ответов extraction и entity resolution."""
from uuid import NAMESPACE_URL, uuid5
import re


_ENTITY_TYPES = {
    "person", "location", "organization", "group", "institution",
    "vehicle", "artifact", "object", "other",
}


def _has_individualizing_marker(surface):
    """Отсечь очевидные standalone generic nouns, не изображая полноценный NER."""
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", surface)
    return any(word[:1].isupper() or any(char.isdigit() for char in word)
               for word in words)


_PRONOUNS = {
    "я", "меня", "мне", "мной", "мною",
    "ты", "тебя", "тебе", "тобой", "тобою",
    "он", "его", "него", "ему", "нему", "им", "ним", "нём",
    "она", "её", "неё", "ней", "нею", "ею",
    "оно",
    "мы", "нас", "нам", "нами",
    "вы", "вас", "вам", "вами",
    "они", "их", "них", "ими", "ними",
}


def _is_pronoun_only(surface):
    """Заглавная буква — не признак имени: местоимение 'я'/'он' в начале
    предложения от первого лица капитализируется точно так же, как имя, а
    `_has_individualizing_marker` этого не различает. Проверяем явно."""
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", surface)
    return bool(words) and all(
        word.casefold().replace("ё", "е") in _PRONOUNS for word in words
    )


def mention_id(book_id, scene_id, text_hash, start, end):
    key = f"{book_id}:{scene_id}:{text_hash}:{start}:{end}"
    return f"mention:{uuid5(NAMESPACE_URL, key).hex}"


def reference_id(book_id, scene_id, text_hash, start, end, target_mention_id):
    key = f"{book_id}:{scene_id}:{text_hash}:{start}:{end}:{target_mention_id}"
    return f"reference:{uuid5(NAMESPACE_URL, key).hex}"


def entity_id(book_id, canonical_name, entity_type=None):
    normalized = " ".join(canonical_name.casefold().split())
    kind = " ".join((entity_type or "").casefold().split())
    return f"entity:{uuid5(NAMESPACE_URL, f'{book_id}:{kind}:{normalized}').hex}"


def surface_spans(text, surface):
    """Все вхождения surface как отдельного слова или словосочетания."""
    pattern = re.compile(rf"(?<!\w){re.escape(surface)}(?!\w)")
    return [(match.start(), match.end()) for match in pattern.finditer(text)]


def locate_occurrence(text, surface, record):
    """Точный полуоткрытый span surface в text или None.

    Приоритет: корректные offsets модели, затем номер вхождения `occurrence`,
    затем единственное вхождение surface. Неоднозначный span отбрасывается.
    """
    start, end = record.get("start_offset"), record.get("end_offset")
    if (type(start) is int and type(end) is int
            and 0 <= start < end <= len(text) and text[start:end] == surface):
        return start, end
    spans = surface_spans(text, surface)
    occurrence = record.get("occurrence")
    if type(occurrence) is int and 1 <= occurrence <= len(spans):
        return spans[occurrence - 1]
    return spans[0] if len(spans) == 1 else None


def normalize_mentions(reply, *, scene):
    """Проверить offsets, однозначно исправить их или отбросить mention."""
    records = reply.get("mentions") if isinstance(reply, dict) else None
    if not isinstance(records, list):
        raise ValueError("Ответ extraction должен содержать список mentions")
    text = scene["text"]
    rows, spans, rejected = [], set(), 0
    local_ids = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            rejected += 1
            continue
        surface = record.get("surface_text")
        span = locate_occurrence(text, surface, record) if isinstance(surface, str) and surface else None
        if span is None:
            rejected += 1
            continue
        start, end = span
        row = {
            "local_id": record.get("local_id", f"a{index + 1}"),
            "surface_text": surface,
            "start_offset": start,
            "end_offset": end,
            "entity_type": None,
            "canonical_name_hint": None,
            "confidence": None,
        }
        if (not isinstance(row["local_id"], str) or not row["local_id"].strip()
                or row["local_id"] in local_ids):
            rejected += 1
            continue
        row["local_id"] = row["local_id"].strip()
        local_ids.add(row["local_id"])
        entity_type = record.get("entity_type")
        if entity_type is not None:
            if (not isinstance(entity_type, str)
                    or entity_type.strip() not in _ENTITY_TYPES):
                rejected += 1
                continue
            row["entity_type"] = entity_type.strip()
        hint = record.get("canonical_name_hint")
        if hint is not None:
            if not isinstance(hint, str) or not hint.strip():
                rejected += 1
                continue
            row["canonical_name_hint"] = hint.strip()
        confidence = record.get("confidence", record.get("extraction_confidence"))
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
                    or not 0 <= confidence <= 1:
                rejected += 1
                continue
            row["confidence"] = float(confidence)
        # Имена, топонимы и именованные артефакты в обычной русской прозе имеют
        # хотя бы один индивидуализирующий маркер. Нижнерегистровые роли и вещи
        # ("урядник", "кибитка", "водка") не должны становиться anchors.
        if not _has_individualizing_marker(surface):
            rejected += 1
            continue
        if _is_pronoun_only(surface):
            rejected += 1
            continue
        if (start, end) in spans:
            rejected += 1
            continue
        spans.add((start, end))
        rows.append(row)
    return (sorted(rows, key=lambda item: (item["start_offset"], item["end_offset"])),
            rejected)


def parse_mentions(reply, *, book_id, scene):
    normalized, _ = normalize_mentions(reply, scene=scene)
    rows = []
    for mention in normalized:
        start, end = mention["start_offset"], mention["end_offset"]
        row = {
            **mention,
            "id": mention_id(book_id, scene["id"], scene["text_hash"], start, end),
            "book_id": book_id,
            "scene_id": scene["id"],
            "scene_text_hash": scene["text_hash"],
        }
        row.pop("local_id", None)
        if row.get("confidence") is not None:
            row["extraction_confidence"] = row.pop("confidence")
        else:
            row.pop("confidence", None)
        rows.append(row)
    return rows


def normalize_references(reply, *, scene, anchor_local_ids, anchor_spans=()):
    records = reply.get("references") if isinstance(reply, dict) else None
    if records is None:
        records = []
    if not isinstance(records, list):
        raise ValueError("Ответ extraction должен содержать список references")
    text, rows, spans, rejected = scene["text"], [], set(), 0
    anchor_spans = set(anchor_spans)
    for record in records:
        if not isinstance(record, dict):
            rejected += 1
            continue
        surface = record.get("surface_text")
        target = record.get("target_local_id")
        kind = record.get("reference_kind")
        if (not isinstance(surface, str) or not surface
                or target not in anchor_local_ids
                or not isinstance(kind, str) or not kind.strip()):
            rejected += 1
            continue
        span = locate_occurrence(text, surface, record)
        if span is None:
            rejected += 1
            continue
        start, end = span
        if (start, end) in spans or (start, end) in anchor_spans:
            rejected += 1
            continue
        spans.add((start, end))
        rows.append({"surface_text": surface, "start_offset": start,
                     "end_offset": end, "target_local_id": target,
                     "reference_kind": kind.strip()})
    return sorted(rows, key=lambda item: (item["start_offset"], item["end_offset"])), rejected


def parse_references(records, *, book_id, scene, normalized_mentions, mentions):
    mention_by_span = {(item["start_offset"], item["end_offset"]): item for item in mentions}
    targets = {
        item["local_id"]: mention_by_span[(item["start_offset"], item["end_offset"])]["id"]
        for item in normalized_mentions
    }
    rows = []
    for item in records:
        target = targets[item["target_local_id"]]
        rows.append({
            **{key: item[key] for key in (
                "surface_text", "start_offset", "end_offset", "reference_kind",
            )},
            "id": reference_id(book_id, scene["id"], scene["text_hash"],
                               item["start_offset"], item["end_offset"], target),
            "book_id": book_id, "scene_id": scene["id"],
            "scene_text_hash": scene["text_hash"], "target_mention_id": target,
        })
    return rows


def parse_resolutions(reply, *, book_id, mentions, entities):
    records = reply.get("resolutions") if isinstance(reply, dict) else None
    if not isinstance(records, list):
        raise ValueError("Ответ resolution должен содержать список resolutions")
    mention_ids = {item["id"] for item in mentions}
    entity_ids = {item["id"] for item in entities}
    seen, resolved, created = set(), [], {}
    for record in records:
        if not isinstance(record, dict) or record.get("mention_id") not in mention_ids:
            raise ValueError("Resolution ссылается на неизвестный mention")
        mention = record["mention_id"]
        if mention in seen:
            raise ValueError("Для mention вернулось несколько решений")
        seen.add(mention)
        choices = sum((record.get("entity_id") is not None,
                       record.get("new_entity") is not None,
                       record.get("unresolved") is True))
        if choices != 1:
            raise ValueError("Для mention нужно выбрать entity, new_entity или unresolved")
        target = record.get("entity_id")
        if target is not None:
            if target not in entity_ids:
                raise ValueError("Resolution ссылается на неизвестную Entity этой книги")
        elif record.get("new_entity") is not None:
            value = record["new_entity"]
            name = value.get("canonical_name") if isinstance(value, dict) else None
            kind = value.get("entity_type") if isinstance(value, dict) else None
            aliases = value.get("aliases", []) if isinstance(value, dict) else None
            if not isinstance(name, str) or not name.strip():
                raise ValueError("У новой Entity нужен canonical_name")
            if kind is not None and (not isinstance(kind, str) or not kind.strip()):
                raise ValueError("entity_type должен быть непустой строкой")
            if not isinstance(aliases, list) or any(
                not isinstance(alias, str) or not alias.strip() for alias in aliases
            ):
                raise ValueError("aliases должен быть списком непустых строк")
            target = entity_id(book_id, name, kind)
            if target not in entity_ids:
                created[target] = {
                    "id": target, "book_id": book_id, "canonical_name": name.strip(),
                    "aliases": list(dict.fromkeys(alias.strip() for alias in aliases)),
                    **({"entity_type": kind.strip()} if kind is not None else {}),
                }
        resolved.append({"mention_id": mention, "entity_id": target})
    if seen != mention_ids:
        raise ValueError("Resolution должен вернуть решение для каждого mention")
    return resolved, list(created.values())
