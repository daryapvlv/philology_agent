"""Нарративная разметка одной сцены: representation/level чанков и события.

Один LLM-вызов на сцену (не на чанк, как extraction упоминаний): модель видит
весь текст сцены с метками [C1]…[Cn] перед каждым TextChunk и список BookEntity
сцены. Якоря событий вычисляются существующей логикой occurrence из
entities.workflow — той же, что уже используется для EntityMention/references, —
а не отдельной второй реализацией. Невалидная форма раздела (chunks/events не
список, событий больше лимита) получает одну попытку исправления с текстом
ошибки. Проблема одного
конкретного события (surface_text не нашёлся дословно, неизвестная сущность и
т.п.) не тратит эту попытку и не валит всю разметку сцены — событие просто
отбрасывается, см. _validate_events.
"""
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5
import json

from llm.requests import request_json

from entities.workflow import locate_occurrence

from .prompt import NARRATIVE_PROMPT

REPRESENTATIONS = {'narrator', 'direct_speech', 'thought', 'document', 'mixed'}
LEVELS = {'primary', 'embedded'}
EMBEDDED_KINDS = {'letter', 'dream', 'story', 'song'}
EVENT_KINDS = {'change_of_state', 'process', 'state'}
MODALITIES = {'actual', 'hypothetical', 'negated', 'iterative', 'dream', 'reported'}
MAX_EVENTS = 7
CONTEXT_CHARS = 300
GIST_CHARS = 300

METHOD_HASH = sha256(NARRATIVE_PROMPT.encode()).hexdigest()[:16]


class NarrativeAnnotationError(ValueError):
    pass


def event_id(book_id, scene_id, text_hash, index):
    key = f'{book_id}:{scene_id}:{text_hash}:{index}'
    return f'event:{uuid5(NAMESPACE_URL, key).hex}'


def marked_scene_text(scene, chunks):
    """Текст сцены с метками [C1]…[Cn] перед каждым TextChunk по порядку чтения."""
    base = scene['start_char']
    parts, cursor = [], 0
    for index, chunk in enumerate(chunks, 1):
        start, end = chunk['start_char'] - base, chunk['end_char'] - base
        parts.append(scene['text'][cursor:start])
        parts.append(f'[C{index}]')
        parts.append(scene['text'][start:end])
        cursor = end
    parts.append(scene['text'][cursor:])
    return ''.join(parts)


def _validate_chunks(content, chunk_count, entity_ids):
    records = content.get('chunks') if isinstance(content, dict) else None
    if not isinstance(records, list):
        raise NarrativeAnnotationError('chunks: нужен список')
    by_index = {}
    for record in records:
        if not isinstance(record, dict):
            raise NarrativeAnnotationError('chunks: каждый элемент — объект')
        marker = record.get('chunk')
        if not isinstance(marker, int) or isinstance(marker, bool) or not 1 <= marker <= chunk_count:
            raise NarrativeAnnotationError(f'chunks: chunk должен быть числом от 1 до {chunk_count}')
        if marker in by_index:
            raise NarrativeAnnotationError(f'chunks: чанк {marker} встречается больше одного раза')
        context = record.get('context')
        if not isinstance(context, str) or not context.strip() or len(context) > CONTEXT_CHARS:
            raise NarrativeAnnotationError(
                f'chunks[{marker}].context: непустая строка до {CONTEXT_CHARS} символов')
        representation = record.get('representation')
        if representation not in REPRESENTATIONS:
            raise NarrativeAnnotationError(
                f'chunks[{marker}].representation: одно из {sorted(REPRESENTATIONS)}')
        level = record.get('level')
        if level not in LEVELS:
            raise NarrativeAnnotationError(f'chunks[{marker}].level: одно из {sorted(LEVELS)}')
        embedded_kind = record.get('embedded_kind')
        if embedded_kind is not None and embedded_kind not in EMBEDDED_KINDS:
            raise NarrativeAnnotationError(
                f'chunks[{marker}].embedded_kind: одно из {sorted(EMBEDDED_KINDS)} или null')
        if level == 'primary' and embedded_kind is not None:
            raise NarrativeAnnotationError(
                f'chunks[{marker}].embedded_kind: должно быть null при level=primary')
        speaker = record.get('speaker_entity_id')
        if speaker is not None and speaker not in entity_ids:
            raise NarrativeAnnotationError(
                f'chunks[{marker}].speaker_entity_id: нет такой сущности в сцене')
        by_index[marker] = dict(chunk=marker, context=context.strip(),
                                representation=representation, speaker_entity_id=speaker,
                                level=level, embedded_kind=embedded_kind)
    missing = sorted(set(range(1, chunk_count + 1)) - set(by_index))
    if missing:
        raise NarrativeAnnotationError(f'chunks: нет записи для {missing}')
    return [by_index[i] for i in range(1, chunk_count + 1)]


def _parse_event(where, record, scene_text, entity_ids):
    if not isinstance(record, dict):
        raise NarrativeAnnotationError(f'{where}: нужен объект')
    surface = record.get('surface_text')
    if not isinstance(surface, str) or not surface.strip():
        raise NarrativeAnnotationError(f'{where}.surface_text: непустая строка')
    span = locate_occurrence(scene_text, surface, record)
    if span is None:
        raise NarrativeAnnotationError(
            f'{where}: surface_text «{surface}» не найден в тексте сцены однозначно '
            'по occurrence')
    gist, gist_roles = record.get('gist'), record.get('gist_roles')
    for field, value in (('gist', gist), ('gist_roles', gist_roles)):
        if not isinstance(value, str) or not value.strip() or len(value) > GIST_CHARS:
            raise NarrativeAnnotationError(
                f'{where}.{field}: непустая строка до {GIST_CHARS} символов')
    kind = record.get('kind')
    if kind not in EVENT_KINDS:
        raise NarrativeAnnotationError(f'{where}.kind: одно из {sorted(EVENT_KINDS)}')
    modality = record.get('modality')
    if modality not in MODALITIES:
        raise NarrativeAnnotationError(f'{where}.modality: одно из {sorted(MODALITIES)}')
    participants = record.get('participants')
    if not isinstance(participants, list) or not participants:
        raise NarrativeAnnotationError(f'{where}.participants: непустой список')
    parsed_participants = []
    for p_index, participant in enumerate(participants):
        if not isinstance(participant, dict):
            raise NarrativeAnnotationError(f'{where}.participants[{p_index}]: нужен объект')
        entity_ref, role = participant.get('entity_id'), participant.get('role')
        if entity_ref not in entity_ids:
            raise NarrativeAnnotationError(
                f'{where}.participants[{p_index}].entity_id: нет такой сущности в сцене')
        if not isinstance(role, str) or not role.strip():
            raise NarrativeAnnotationError(f'{where}.participants[{p_index}].role: непустая строка')
        parsed_participants.append(dict(entity_id=entity_ref, role=role.strip()))
    locations = {}
    for field in ('from_location', 'to_location'):
        value = record.get(field)
        if value is not None and value not in entity_ids:
            raise NarrativeAnnotationError(f'{where}.{field}: нет такой сущности в сцене')
        locations[field] = value
    return dict(surface_text=surface, start_offset=span[0], end_offset=span[1],
               gist=gist.strip(), gist_roles=gist_roles.strip(), kind=kind,
               modality=modality, participants=parsed_participants, **locations)


def _validate_events(content, scene_text, entity_ids):
    """rows валидных событий, skipped — короткие причины отброшенных.

    events — заведомо переменной длины (модель сама решает, сколько событий
    значимо, вплоть до пустого списка), поэтому проблема одного события —
    ненайденный дословно surface_text, неизвестная сущность, невалидное
    перечисление — отбрасывает только это событие, а не всю разметку сцены.
    Иначе единственный неточный якорь модели раз за разом ломал бы весь этап
    при каждом повторном запуске (кэш LLM-ответа детерминирован по payload,
    так что без этого разбора «отбросить и пересчитать сцену снова» ничего
    не меняется без ручного вмешательства). Форма самого раздела — не список,
    слишком много записей — остаётся фатальной и уходит в errors на повтор:
    это единственное, что стоит переспрашивать у модели.
    """
    records = content.get('events') if isinstance(content, dict) else None
    if not isinstance(records, list):
        raise NarrativeAnnotationError('events: нужен список')
    if len(records) > MAX_EVENTS:
        raise NarrativeAnnotationError(f'events: не больше {MAX_EVENTS} на сцену')
    rows, skipped = [], []
    for index, record in enumerate(records):
        try:
            rows.append(_parse_event(f'events[{index}]', record, scene_text, entity_ids))
        except NarrativeAnnotationError as error:
            skipped.append(str(error))
    return rows, skipped


def annotate_scene(client, config, scene, chunks, entities, *, cache_dir=None, usage=None):
    """Один LLM-вызов на сцену; при невалидном ответе (форма разделов) — одна
    попытка исправления с текстом ошибки. Возвращает {'chunks': [...],
    'events': [...], 'skipped_events': [...]} без id — их присваивает
    вызывающий код (репозиторий пишет их вместе со scene/book_id).
    skipped_events — причины отброшенных отдельных событий (см. _validate_events);
    они не фатальны и не тратят вторую попытку."""
    if not chunks:
        raise ValueError('У сцены нет TextChunk; сначала выполни этап chunking')
    usage = usage if usage is not None else {'calls': 0, 'tokens': 0, 'cache_hits': 0}
    entity_ids = {entity['id'] for entity in entities}
    payload_entities = [dict(id=entity['id'], name=entity.get('canonical_name'),
                             type=entity.get('entity_type')) for entity in entities]
    text = marked_scene_text(scene, chunks)
    errors = []
    for _ in range(2):
        payload = json.dumps(dict(scene_id=scene['id'], text=text,
                                  entities=payload_entities, errors=errors), ensure_ascii=False)
        raw = request_json(client, config, NARRATIVE_PROMPT, payload, cache_dir, usage)
        if raw is None:
            raise NarrativeAnnotationError('Бюджет разметки сцены исчерпан')
        try:
            if raw['finish_reason'] != 'stop':
                raise NarrativeAnnotationError(
                    f"Ответ разметки оборван (finish_reason={raw['finish_reason']})")
            content = json.loads(raw['content'])
            chunk_rows = _validate_chunks(content, len(chunks), entity_ids)
            event_rows, skipped_events = _validate_events(content, scene['text'], entity_ids)
            return dict(chunks=chunk_rows, events=event_rows, skipped_events=skipped_events)
        except json.JSONDecodeError as error:
            errors = [f'Ответ не является JSON: {error}']
        except NarrativeAnnotationError as error:
            errors = [str(error)]
    raise NarrativeAnnotationError(
        'Не удалось получить корректную нарративную разметку сцены: ' + '; '.join(errors))
