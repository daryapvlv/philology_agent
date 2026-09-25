"""Связи между событиями книги: retells/prefigures/causes/parallels/none поверх
NarrativeEvent.

Shortlist считает код без LLM (эмбеддинги gist_roles с кэшем на самом
NarrativeEvent + бонус за общих участников + порог сходства), LLM батчами
классифицирует только отобранные пары — по образцу reconciliation в
entities.chapter_resolution/entities.book_resolution: эвристика только
ранжирует и отбирает кандидатов, финальное решение по типу связи всегда за
моделью, а невалидный или отсутствующий в ответе результат по умолчанию
становится "none" (без связи), а не догадкой.

Соседи ищутся по двум эмбеддингам: обезличенный gist_roles — для параллелей
(сходная структура ролей), gist с именами — для пересказов и причин (те же
участники). Отдельно в shortlist попадают события в прямой речи, документе,
мыслях, вложенном рассказе или с modality=reported вместе с лучшими событиями
других сцен, где есть общий участник: кандидаты в пересказы (письма, слухи).
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import json
import math
import operator

from llm.requests import json_content, request_json

from retrieval.indexing.service import vectors

from .links_prompt import NARRATIVE_LINK_PROMPT

LINK_TYPES = {'retells', 'prefigures', 'causes', 'parallels', 'none'}
# Подтипы — по нарратологическим различениям (повторяющее повествование,
# причинная сеть Трабассо, параллелизм/контраст); неизвестный подтип не
# отменяет связь, а становится None.
SUBTYPES = {
    'retells': {'speech', 'letter', 'rumor', 'memory', 'summary'},
    'prefigures': {'dream', 'prophecy', 'omen', 'promise', 'hint'},
    'causes': {'physical', 'motivational', 'psychological', 'enabling'},
    'parallels': {'situation', 'roles', 'motif', 'contrast'},
}
DIRECTED_TYPES = {'retells', 'prefigures', 'causes'}
DIRECTIONS = {'left_to_right', 'right_to_left'}
# explicit — сказано в тексте, inferred — восстанавливается читателем,
# suggestive — интерпретационная гипотеза для проверки исследователем.
BASES = {'explicit', 'inferred', 'suggestive'}
REASON_CHARS = 200

LINKS_METHOD_HASH = sha256(NARRATIVE_LINK_PROMPT.encode()).hexdigest()[:16]


class NarrativeLinkError(ValueError):
    pass


def _context(text, start, end, limit):
    side = max(0, (limit - (end - start)) // 2)
    left, right = max(0, start - side), min(len(text), end + side)
    return text[left:right]


def _containing_chunk(chunks, scene_start, start, end):
    for chunk in chunks:
        local_start, local_end = chunk['start_char'] - scene_start, chunk['end_char'] - scene_start
        if local_start <= start and end <= local_end:
            return chunk
    return None


def enrich_events(scenes, events, participants, chunks, *, context_chars):
    """Чистая функция без Neo4j: сырые NarrativeEvent + сцены/чанки книги ->
    события с контекстом якоря, участниками и признаками пересказа. Событие
    сцены, которой больше нет среди актуальных (готовых) сцен, отбрасывается —
    так и должно быть по инварианту 3: смена статуса сцены уже удалила его
    узел, здесь просто защита от рассинхронизации в самой выборке."""
    scenes_by_id = {scene['id']: scene for scene in scenes}
    chunks_by_scene = {}
    for chunk in chunks:
        chunks_by_scene.setdefault(chunk['scene_id'], []).append(chunk)
    for scene_chunks in chunks_by_scene.values():
        scene_chunks.sort(key=lambda chunk: chunk['start_char'])
    enriched = []
    for event in events:
        scene = scenes_by_id.get(event.get('scene_id'))
        if scene is None:
            continue
        start, end = event['start_offset'], event['end_offset']
        chunk = _containing_chunk(chunks_by_scene.get(scene['id'], []), scene['start_char'], start, end)
        enriched.append(dict(
            event,
            position=(scene.get('start_char') or 0) + start,
            scene_title=scene.get('title') or '',
            context=_context(scene['text'], start, end, context_chars),
            participant_ids=participants.get(event['id'], []),
            chunk_level=chunk['level'] if chunk else None,
            chunk_representation=chunk['representation'] if chunk else None,
        ))
    return enriched


EMBEDDED_FIELDS = ('gist_roles', 'gist')


def embed_events(events, embedder, *, field='gist_roles', target='embedding', batch_size=32):
    """Эмбеддинг поля события (gist_roles — обезличенный, для параллелей; gist — с
    именами, для пересказов и причин) с кэшем по (hash, model), хранимым прямо на
    NarrativeEvent в {field}_embedding*. Без embedder события возвращаются без
    target: shortlist_pairs всё равно найдёт кандидатов в пересказы по общим
    участникам — отсутствие embeddings не подменяется скрытым fallback."""
    if field not in EMBEDDED_FIELDS:
        raise ValueError(f'field: одно из {EMBEDDED_FIELDS}')
    if embedder is None:
        return {'events': events, 'embedded': 0, 'to_persist': []}
    model = embedder.model
    if not model.strip():
        raise ValueError('Укажи стабильное имя embedding-модели')
    prefix = f'{field}_embedding'
    hashes = {event['id']: sha256((event.get(field) or '').encode()).hexdigest()
              for event in events if (event.get(field) or '').strip()}
    cache = {}
    for event in events:
        digest = hashes.get(event['id'])
        if (digest and event.get(f'{prefix}_hash') == digest
                and event.get(f'{prefix}_model') == model and event.get(prefix)):
            cache[digest] = event[prefix]
    texts = {hashes[event['id']]: event[field] for event in events if event['id'] in hashes}
    missing = list(dict.fromkeys(digest for digest in hashes.values() if digest not in cache))
    embedded = 0
    for start in range(0, len(missing), batch_size):
        keys = missing[start:start + batch_size]
        values = vectors(embedder.embed_documents([texts[key] for key in keys]), len(keys))
        cache.update(zip(keys, values))
        embedded += len(keys)
    enriched, to_persist = [], []
    for event in events:
        digest = hashes.get(event['id'])
        embedding = cache.get(digest) if digest else None
        if embedding is None:
            enriched.append(event)
            continue
        enriched.append(dict(event, **{target: embedding}))
        if event.get(f'{prefix}_hash') != digest or event.get(f'{prefix}_model') != model:
            to_persist.append(dict(id=event['id'], embedding=embedding, model=model, hash=digest))
    return {'events': enriched, 'embedded': embedded, 'to_persist': to_persist}


RETELLING_REPRESENTATIONS = {'direct_speech', 'document', 'thought'}


def _unit(vector):
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else None


def _shared_participants(left, right):
    return bool(set(left.get('participant_ids', ())) & set(right.get('participant_ids', ())))


def _is_retelling_side(event):
    return (event.get('modality') == 'reported'
            or event.get('chunk_level') == 'embedded'
            or event.get('chunk_representation') in RETELLING_REPRESENTATIONS)


def _similarities(events, key):
    """Косинус всех пар событий разных сцен по эмбеддингу key: {(i, j): score}, i < j."""
    units = {i: _unit(event[key]) for i, event in enumerate(events) if event.get(key)}
    units = {i: vector for i, vector in units.items() if vector is not None}
    indexes = sorted(units)
    result = {}
    for position, i in enumerate(indexes):
        left = units[i]
        for j in indexes[position + 1:]:
            if events[i]['scene_id'] != events[j]['scene_id']:
                result[i, j] = sum(map(operator.mul, left, units[j]))
    return result


def _top_neighbours(events, scores, *, top_k, floor):
    """Для каждого события — не больше top_k лучших соседей со счётом не ниже floor."""
    by_event = {}
    for (i, j), score in scores.items():
        if score >= floor:
            by_event.setdefault(i, []).append((score, j))
            by_event.setdefault(j, []).append((score, i))
    chosen = {}
    for i, neighbours in by_event.items():
        neighbours.sort(key=lambda item: (-item[0], events[item[1]]['id']))
        for score, j in neighbours[:top_k]:
            key = (min(i, j), max(i, j))
            chosen[key] = max(score, chosen.get(key, score))
    return chosen


def shortlist_pairs(events, *, top_k, similarity_threshold, limit_factor, participant_bonus,
                    known=()):
    """Кандидатные пары для классификации; тип связи решает только LLM.

    - параллели: ближайшие соседи по обезличенному gist_roles (event['embedding']);
    - та же история (пересказ, причина): ближайшие соседи по gist с именами
      (event['story_embedding']) плюс бонус за общего участника;
    - пересказы: для событий в прямой речи, документе, мыслях, вложенном рассказе или
      с modality=reported — лучшие top_k событий других сцен с общим участником,
      независимо от порога;
    - known: пары id уже найденных связей и пары событий из связей между сценами —
      обязательные кандидаты.
    similarity_threshold — нижняя граница для ближайших соседей, а не отбор всех пар
    выше неё: при плоском распределении близости жёсткий порог почти ничего не пропускал."""
    if type(top_k) is not int or not 1 <= top_k <= 20:
        raise ValueError('top_k должен быть от 1 до 20')
    merged = _top_neighbours(events, _similarities(events, 'embedding'),
                             top_k=top_k, floor=similarity_threshold)
    story = _similarities(events, 'story_embedding')
    boosted = {key: score + (participant_bonus if _shared_participants(events[key[0]], events[key[1]])
                             else 0.0) for key, score in story.items()}
    for key, score in _top_neighbours(events, boosted, top_k=top_k,
                                      floor=similarity_threshold).items():
        merged[key] = max(score, merged.get(key, score))

    retelling = set()
    for i, source in enumerate(events):
        if not _is_retelling_side(source):
            continue
        partners = [j for j, candidate in enumerate(events)
                    if j != i and candidate['scene_id'] != source['scene_id']
                    and _shared_participants(source, candidate)]
        partners.sort(key=lambda j: (-story.get((min(i, j), max(i, j)), 0.0), events[j]['id']))
        retelling.update((min(i, j), max(i, j)) for j in partners[:top_k])
    index = {event['id']: i for i, event in enumerate(events)}
    for left_id, right_id in known:
        if left_id in index and right_id in index and left_id != right_id:
            retelling.add((min(index[left_id], index[right_id]), max(index[left_id], index[right_id])))
    for key in retelling:
        merged.setdefault(key, similarity_threshold)
    if not merged:
        return []
    limit = max(len(retelling), round(limit_factor * len(events)))
    ordered = sorted(merged, key=lambda key: (key not in retelling, -merged[key],
                                              events[key[0]]['id'], events[key[1]]['id']))
    return [in_reading_order(events[i], events[j]) for i, j in ordered[:limit]]


def in_reading_order(left, right):
    """Пара так, чтобы left стоял в тексте раньше: промпт опирается на порядок
    сторон, когда решает направление пересказа, предвестия и причины."""
    if (right.get('position', 0), right['id']) < (left.get('position', 0), left['id']):
        return right, left
    return left, right


SCENE_LINK_LABELS = dict(retells='пересказ', prefigures='предвестие', causes='причина',
                         parallels='параллель')


def scene_link_pairs(events, scene_links, *, per_link):
    """Пары событий для каждой связи между сценами: по per_link самых близких
    пар (эмбеддинги gist и gist_roles, общий участник) из событий двух сцен.
    Возвращает (pairs, hints): pairs — [(left_id, right_id)] для shortlist
    (known), hints — {frozenset(scene_ids): текст связи сцен} для классификатора."""
    by_scene = {}
    for event in events:
        by_scene.setdefault(event['scene_id'], []).append(event)
    pairs, hints = [], {}
    for link in scene_links:
        left, right = by_scene.get(link['source_id'], []), by_scene.get(link['target_id'], [])
        if not left or not right:
            continue
        hints[frozenset((link['source_id'], link['target_id']))] = (
            f"{SCENE_LINK_LABELS[link['type']]} между сценами: {link['reason']}".strip())
        scored = []
        for a in left:
            for b in right:
                score = max(_cosine(a.get('story_embedding'), b.get('story_embedding')),
                            _cosine(a.get('embedding'), b.get('embedding')))
                scored.append((-(score + (0.05 if _shared_participants(a, b) else 0.0)),
                               a['id'], b['id']))
        pairs += [(a_id, b_id) for _, a_id, b_id in sorted(scored)[:per_link]]
    return pairs, hints


def _cosine(left, right):
    if not left or not right:
        return 0.0
    left, right = _unit(left), _unit(right)
    return sum(map(operator.mul, left, right)) if left and right else 0.0


def _pair_id(left, right):
    return f'{left["id"]}::{right["id"]}'


def _side(event):
    return {'gist': event['gist'], 'roles': event.get('gist_roles') or '',
            'context': event['context'], 'scene': event.get('scene_title') or '',
            'modality': event.get('modality') or 'actual',
            'form': event.get('chunk_representation') or 'narrator'}


def _pair_payload(pair_id, left, right, hint=None):
    payload = {'pair_id': pair_id, 'left': _side(left), 'right': _side(right)}
    if hint:
        payload['hint'] = hint
    return payload


def _reason(value):
    if not isinstance(value, str):
        return ''
    text = ' '.join(value.split())
    return text if len(text) <= REASON_CHARS else text[:REASON_CHARS - 1].rstrip() + '…'


def _confidence(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value:
        return None
    return min(1.0, max(0.0, float(value)))


def _parse_decisions(content, expected):
    """expected: {pair_id: (left_id, right_id)}. Без явного допустимого type решение —
    'none' (связь не угадывается). Оформление не отменяет найденную связь: длинная
    причина обрезается, неизвестные subtype/basis становятся None, confidence
    (если модель его дала) приводится к [0, 1], direction у parallels
    игнорируется, направленная связь без понятного направления остаётся
    ненаправленной. Дубликат pair_id — первая запись с допустимым type."""
    parsed = {}
    rows = content.get('decisions') if isinstance(content, dict) else None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            pair_id = row.get('pair_id')
            if pair_id not in expected or pair_id in parsed:
                continue
            link_type = row.get('type')
            if link_type not in LINK_TYPES:
                continue
            direction = row.get('direction') if link_type in DIRECTED_TYPES else None
            subtype = row.get('subtype')
            basis = row.get('basis') if link_type != 'none' else None
            parsed[pair_id] = dict(pair_id=pair_id, type=link_type,
                                   subtype=subtype if subtype in SUBTYPES.get(link_type, ()) else None,
                                   direction=direction if direction in DIRECTIONS else None,
                                   basis=basis if basis in BASES else None,
                                   confidence=_confidence(row.get('confidence')),
                                   reason=_reason(row.get('reason')))
    return [parsed.get(pair_id, dict(pair_id=pair_id, type='none', subtype=None, direction=None,
                                     basis=None, confidence=0.0, reason=''))
            for pair_id in expected]


def _batches(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _classify_batch(client, config, batch, cache_dir, usage, hints):
    expected, payload_pairs = {}, []
    for left, right in batch:
        pair_id = _pair_id(left, right)
        expected[pair_id] = (left['id'], right['id'])
        payload_pairs.append(_pair_payload(
            pair_id, left, right,
            hints.get(frozenset((left.get('scene_id'), right.get('scene_id')))) if hints else None))
    payload = json.dumps({'pairs': payload_pairs}, ensure_ascii=False)
    raw = request_json(client, config, NARRATIVE_LINK_PROMPT, payload, cache_dir, usage)
    if raw is None:
        raise NarrativeLinkError('Бюджет классификации связей событий исчерпан')
    try:
        content = json_content(raw)
    except json.JSONDecodeError:
        content = None
    return _parse_decisions(content, expected)


def classify_links(client, config, pairs, *, cache_dir=None, usage=None, hints=None):
    """pairs: [(left_event, right_event)]. Батчи по config.link_batch_size,
    параллельно по config.link_workers; порядок решений — порядок пар. hints —
    {frozenset(scene_ids): связь сцен}: гипотеза обзора сюжета для пары событий
    из этих сцен, решение всё равно за классификатором."""
    usage = usage if usage is not None else {'calls': 0, 'tokens': 0, 'cache_hits': 0}
    batches = list(_batches(pairs, config.link_batch_size))
    if not batches:
        return []
    call_config = replace(config, max_calls=max(config.max_calls, len(batches)))
    workers = max(1, min(getattr(config, 'link_workers', 1), len(batches)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda batch: _classify_batch(client, call_config, batch, cache_dir, usage,
                                          hints or {}), batches))
    return [decision for decisions in results for decision in decisions]


def build_link_rows(decisions, expected, method_hash):
    """decisions с type='none' не порождают ребро. У направленных типов (retells,
    prefigures, causes) с направлением source — пересказывающее / предвестие /
    причина, target — пересказанное / осуществление / следствие (directed=True);
    без направления и у parallels пара хранится в исходном порядке и читается
    как ненаправленная."""
    rows = []
    for decision in decisions:
        if decision['type'] == 'none':
            continue
        left_id, right_id = expected[decision['pair_id']]
        directed = decision['type'] in DIRECTED_TYPES and decision['direction'] is not None
        if directed and decision['direction'] == 'right_to_left':
            source_id, target_id = right_id, left_id
        else:
            source_id, target_id = left_id, right_id
        rows.append(dict(source_id=source_id, target_id=target_id, type=decision['type'],
                         subtype=decision.get('subtype'), basis=decision.get('basis'),
                         directed=directed, confidence=decision['confidence'],
                         reason=decision['reason'], method_hash=method_hash))
    return rows


def resolve_links(client, config, pairs, *, cache_dir=None, usage=None, hints=None):
    """Классифицировать отобранные пары и превратить решения в готовые для
    записи рёбра. Возвращает (rows, decisions)."""
    expected = {_pair_id(left, right): (left['id'], right['id']) for left, right in pairs}
    decisions = classify_links(client, config, pairs, cache_dir=cache_dir, usage=usage,
                               hints=hints)
    rows = build_link_rows(decisions, expected, LINKS_METHOD_HASH)
    return rows, decisions
