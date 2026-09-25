"""Поиск кандидатов без LLM: слова — все вхождения, описания — ближайшие по смыслу."""
from dataclasses import dataclass, field

from neo4j.exceptions import ClientError

NARRATIVE_ANCHORS = 10
BASELINE_NARRATIVE_CHANNELS = ('discourse',)
ENTITY_NARRATIVE_CHANNELS = ('character', 'chronotope', 'soft_character')
LINK_NARRATIVE_CHANNELS = ('retelling', 'prefiguring', 'parallel', 'causal')
MERGE_LIMIT = 20
SEMANTIC_DEPTH = 15
SCENES_PER_PASSAGES = 5
SCENE_WORD_LIMIT = 5


CHANNEL_ERRORS = (ValueError, ClientError)


def _channel_warning(label, query, error):
    if isinstance(error, ClientError):
        if 'no such fulltext schema index' in str(error):
            return (f'канал «{label}» пропущен: в базе нет его индекса — выполните '
                    'python -m infrastructure.neo4j.migrate')
        return f'канал «{label}» пропущен: ошибка Neo4j {error.code}'
    return f'{label} «{query}»: {error}'


@dataclass
class Retrieval:
    search_id: str | None
    candidate_count: int = 0
    warnings: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


def _entity_ids(search, names, mode, warnings, assumptions):
    ids = []
    for name in names:
        found = search.find_entities(name, limit=5)
        if not found['items']:
            warnings.append(f'сущность «{name}» не найдена в графе книги')
            continue
        best = found['items'][0]
        ids.append(best['id'])
        if best['canonical_name'].casefold() != name.casefold():
            others = [row['canonical_name'] for row in found['items'][1:3]]
            note = f'«{name}» понято как «{best["canonical_name"]}»'
            if found['ambiguous'] and others:
                note += ' (другие варианты: ' + ', '.join(f'«{o}»' for o in others) + ')'
            assumptions.append(note)
    if mode == 'intersection' and len(ids) != len(names):
        return []
    return list(dict.fromkeys(ids))


def _word_channels(search, words, section_ids, limit, warnings):
    """Все вхождения слов: по тексту, по описаниям сцен (не глубже SCENE_WORD_LIMIT —
    сцена раскрывается во все свои отрывки) и по нарративному контексту чанка."""
    ids = []
    for word in words:
        for target, label, channel_limit in (
                ('text', 'слова', limit), ('scenes', 'слова по сценам', SCENE_WORD_LIMIT),
                ('context', 'слова по контексту', limit)):
            try:
                ids.append(search.search_words(word, target=target, section_ids=section_ids,
                                               candidate_limit=channel_limit,
                                               page_size=1)['search_id'])
            except CHANNEL_ERRORS as error:
                warnings.append(_channel_warning(label, word, error))
    return ids


def _semantic_channels(search, descriptions, section_ids, depth, warnings):
    """Ближайшие по смыслу отрывки и сцены; глубину задаёт число результатов.
    Оценки Neo4j — (1 + cos) / 2, порог по ним пока не откалиброван."""
    if not descriptions:
        return []
    if search.embedder is None:
        warnings.append('смысловой поиск не запускался: у книги нет эмбеддингов')
        return []
    ids = []
    for description in descriptions:
        try:
            ids.append(search.search_semantic(
                description, section_ids=section_ids, candidate_limit=depth,
                scene_limit=max(1, depth // SCENES_PER_PASSAGES), page_size=1)['search_id'])
        except CHANNEL_ERRORS as error:
            warnings.append(_channel_warning('смысл', description, error))
    return ids


def effective_channels(channels, status):
    """Дешёвые каналы обхода добавляются всегда, когда для них есть данные."""
    result = set(channels) | set(BASELINE_NARRATIVE_CHANNELS)
    if status['entity_graph_ready']:
        result |= set(ENTITY_NARRATIVE_CHANNELS)
    if status['narrative_links_ready']:
        result |= set(LINK_NARRATIVE_CHANNELS)
    return sorted(result)


def expand_from(search, anchor_search_id, channels, *, review_status, limit):
    page = search.continue_search(anchor_search_id, offset=0, page_size=NARRATIVE_ANCHORS,
                                  review_status=review_status)
    anchors = [item['id'] for item in page['items']]
    if not anchors:
        return None
    return search.expand_narrative(search_id=anchor_search_id, candidate_ids=anchors,
                                   channels=effective_channels(channels, search.index_status()),
                                   candidate_limit=limit, page_size=1)['search_id']


def merge(search, ids, query=None):
    while len(ids) > 1:
        ids = [group[0] if len(group) == 1 else
               search.merge_results(group, page_size=1, query=query)['search_id']
               for group in (ids[i:i + MERGE_LIMIT] for i in range(0, len(ids), MERGE_LIMIT))]
    return ids[0]


def retrieve(search, *, criterion, words=(), descriptions=(), entities=(),
             entity_mode='intersection', scope=(), expand=(), candidate_limit=150,
             semantic_depth=SEMANTIC_DEPTH):
    """Кандидаты по словам, описаниям и сущностям, опционально с обходом от найденного;
    результат спроецирован на отрывки и помечен критерием."""
    section_ids = list(scope) or None
    warnings, assumptions = [], []
    ids = _word_channels(search, list(words), section_ids, candidate_limit, warnings)
    ids += _semantic_channels(search, list(descriptions), section_ids, semantic_depth, warnings)
    if entities:
        try:
            entity_ids = _entity_ids(search, list(entities), entity_mode, warnings, assumptions)
            if entity_ids:
                ids.append(search.get_scenes_for_entities(
                    entity_ids, mode=entity_mode, section_ids=section_ids,
                    candidate_limit=candidate_limit, page_size=1)['search_id'])
        except CHANNEL_ERRORS as error:
            warnings.append(_channel_warning('сущности', '', error))
    if expand and ids:
        try:
            expanded = expand_from(search, merge(search, ids), expand, review_status=None,
                                   limit=candidate_limit)
            if expanded:
                ids.append(expanded)
        except CHANNEL_ERRORS as error:
            warnings.append(_channel_warning('обход', '', error))
    if not ids:
        return Retrieval(None, warnings=warnings, assumptions=assumptions)
    projected = search.project_passages(merge(search, ids), query=criterion, page_size=1)
    warnings = list(dict.fromkeys([*warnings, *projected.get('warnings', [])]))
    return Retrieval(projected['search_id'], projected['candidate_count'], warnings, assumptions)
