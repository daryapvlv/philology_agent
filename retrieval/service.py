"""Независимые инструменты поиска. Ни один инструмент не запускает другой."""
from copy import deepcopy
from datetime import datetime, timezone
from difflib import SequenceMatcher
import re
from uuid import uuid4

from structure.graph_db.repository import ConflictError, get_repository
from .indexing import IndexService, vectors
from .ranges import searchable_ranges
from .repository import SearchRepository
from .words import lexical_query


_SENTENCE_BREAK = re.compile(r'[.!?…]+[»")\]]*(?=\s|$)|\n')


def _channel_rank(score):
    """Ранг кандидата внутри канала, восстановленный из RRF-очков 1/(60+rank).

    Все score в этом модуле строятся именно по этой формуле (см. _results,
    search_semantic, expand_narrative, merge_results), так что обратное
    преобразование точное."""
    return round(1 / score) - 60


def _round_robin_order(items):
    """Порядок кандидатов вместо чистого RRF: по очереди берём следующего
    непройденного кандидата из каждого канала по его собственному рангу.

    Так кандидат, найденный только одним "слабым" по фьюжну каналом (например
    только semantic), не тонет за спиной кандидатов с большим числом каналов —
    он получает свою очередь наравне с остальными. Порядок детерминирован и
    сохраняется как есть в сессии поиска."""
    channel_lists = {}
    for item in items:
        for channel, score in item['scores'].items():
            channel_lists.setdefault(channel, []).append((_channel_rank(score), item['id']))
    for entries in channel_lists.values():
        entries.sort(key=lambda pair: pair[0])
    channels = sorted(channel_lists)
    cursors = {channel: 0 for channel in channels}
    order, seen = [], set()
    while len(seen) < len(items):
        progressed = False
        for channel in channels:
            entries = channel_lists[channel]
            cursor = cursors[channel]
            while cursor < len(entries) and entries[cursor][1] in seen:
                cursor += 1
            if cursor < len(entries):
                item_id = entries[cursor][1]
                seen.add(item_id)
                order.append(item_id)
                cursor += 1
                progressed = True
            cursors[channel] = cursor
        if not progressed:
            break
    return order


def _evidence_span(text, evidence):
    """Диапазон предложения(й) в text, содержащих дословную выдержку evidence.

    Сравнение по словам: регистр, ё/е, пробелы и пунктуация между словами не
    важны — модель часто меняет кавычки или переносы. Одно короткое слово не
    считается выдержкой: его совпадение ничего не доказывает."""
    words = re.findall(r'\w+', evidence.replace('ё', 'е').replace('Ё', 'Е'))
    if len(words) < 2 and not (words and len(words[0]) >= 4):
        return None
    haystack = text.replace('ё', 'е').replace('Ё', 'Е')
    pattern = r'(?<!\w)' + r'\W+'.join(map(re.escape, words)) + r'(?!\w)'
    match = re.search(pattern, haystack, re.IGNORECASE)
    if match is None:
        return None
    breaks = list(_SENTENCE_BREAK.finditer(text))
    start = max((b.end() for b in breaks if b.end() <= match.start()), default=0)
    end = min((b.end() for b in breaks if b.end() >= match.end()), default=len(text))
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end



LINK_LABELS = dict(retells='пересказ того же события', prefigures='предвестие',
                   causes='связь по причине', parallels='параллель')


def _link_path(row, target, *, scenes=False, reason=False):
    """Человекочитаемое «как найдено» для связи: тип, уровень, основание, цель."""
    label = LINK_LABELS[row['link_type']] + (' между сценами' if scenes else '')
    if row.get('basis') == 'suggestive':
        label += ' (гипотеза)'
    tail = f' — {row["reason"]}' if reason and row.get('reason') else ''
    return f'{label}: «{target}»{tail}'


class SearchService:
    def __init__(self, book_id, repository=None, embedder=None, research_id="default",
                 sessions=None):
        self.book_id = book_id
        self.books = repository or get_repository(initialize=False)
        self.repository = SearchRepository(self.books)
        self.embedder = embedder
        if not isinstance(research_id, str) or not research_id or len(research_id) > 200:
            raise ValueError('Нужен research_id до 200 символов')
        if sessions is None:
            raise ValueError('Передай хранилище research sessions в SearchService')
        self.sessions = sessions

    def _scope(self, section_ids, page_size, candidate_limit):
        self._page_size(page_size)
        if type(candidate_limit) is not int or not 1 <= candidate_limit <= 2000:
            raise ValueError('candidate_limit должен быть от 1 до 2000')
        if section_ids is not None and (not isinstance(section_ids, (list, tuple)) or not section_ids
                                       or not all(isinstance(s, str) for s in section_ids)):
            raise ValueError('section_ids — непустой список ID; None означает всю книгу')
        return self.repository.scope(self.book_id, section_ids)

    @staticmethod
    def _query(query):
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ValueError('Нужен непустой запрос до 1000 символов')
        return query.strip()

    @staticmethod
    def _require_index(scope):
        if not scope['generation'] or not scope['current']:
            raise ValueError('Индекс не готов. Подготовь книгу через retrieval.indexing.IndexService.index_book()')

    def index_status(self):
        """Состояние поискового индекса без чтения текста книги."""
        state, _, _ = self.repository.scope(self.book_id, None)
        fields = ('revision', 'generation', 'current', 'model', 'chunk_count',
                  'embedded_count', 'scene_count', 'scene_embedded_count')
        result = {key: state[key] for key in fields}
        graph = self.repository.entity_graph_status(self.book_id)
        result['entity_graph_ready_scenes'] = graph['covered_scenes'] if graph else 0
        result['entity_graph_total_ready_scenes'] = graph['ready_scenes'] if graph else 0
        result['entity_graph_ready'] = bool(graph and graph['ready_scenes']
                                            and graph['covered_scenes'] == graph['ready_scenes'])
        result['narrative_links_ready'] = bool(self.repository.narrative_links_status(self.book_id))
        return result

    def index_book(self, *, with_embeddings=False):
        """Явно перестроить поисковые данные книги."""
        if type(with_embeddings) is not bool:
            raise ValueError('with_embeddings должен быть bool')
        if with_embeddings and self.embedder is None:
            raise ValueError('Embedding-провайдер не настроен')
        return IndexService(self.book_id, self.books,
                            self.embedder if with_embeddings else None).index_book()

    def search_words(self, query, *, target='text', section_ids=None, candidate_limit=200, page_size=10):
        """Слова через OR; target: text, scenes или context (TextChunk.narrative_context,
        стадия 8). Без эмбеддингов и обхода графа."""
        query = self._query(query)
        if target not in {'text', 'scenes', 'context'}:
            raise ValueError('target должен быть text, scenes или context')
        scope = self._scope(section_ids, page_size, candidate_limit)
        self._require_index(scope[0])
        rows = self.repository.words(self.book_id, scope[2], lexical_query(query), target, candidate_limit)
        return self._results(scope, section_ids, query, rows, f'words:{target}:{query}',
                             target == 'scenes', candidate_limit, page_size)

    def search_semantic(self, query, *, section_ids=None, candidate_limit=200, page_size=10,
                        scene_limit=None):
        """RRF двух каналов: близость самих отрывков и близость описаний сцен.

        Текстовый канал ранжирует отрывки и не требует построенных сцен; канал
        описаний даёт диапазоны сцен — их сводит к отрывкам project_passages.
        scene_limit ограничивает число сцен отдельно: каждая сцена превращается во
        все свои отрывки, поэтому глубокий канал сцен быстро покрывает всю книгу."""
        scene_limit = candidate_limit if scene_limit is None else scene_limit
        if type(scene_limit) is not int or not 0 <= scene_limit <= candidate_limit:
            raise ValueError('scene_limit должен быть от 0 до candidate_limit')
        query = self._query(query)
        scope = self._scope(section_ids, page_size, candidate_limit)
        self._require_index(scope[0])
        book = scope[0]
        if self.embedder is None:
            raise ValueError('Для семантического поиска передай embedder')
        if book['model'] != self.embedder.model or not book['embedded_count']:
            raise ValueError('Нет эмбеддингов нужной модели; подготовь индекс через retrieval.indexing.IndexService')
        vector = vectors([self.embedder.embed_query(query)], 1)[0]
        if book['dimensions'] != len(vector):
            raise ValueError('Размерность запроса отличается от индекса')
        warnings = []
        if book['embedded_count'] != book['chunk_count']:
            warnings.append('Эмбеддинги есть не у всех фрагментов книги')
        text_rows = self.repository.semantic(self.book_id, scope[2], vector, book['model'],
                                             candidate_limit)
        summary_rows = self.repository.scene_summaries(
            self.book_id, scope[2], vector, book['model'], scene_limit) if scene_limit else []
        if scene_limit and not summary_rows:
            warnings.append('Нет эмбеддингов описаний сцен; использован только текстовый канал')
        fused = {}
        channels = ((f'semantic:text:{query}', text_rows, candidate_limit),
                    (f'semantic:summary:{query}', summary_rows, scene_limit))
        for channel, channel_rows, limit in channels:
            for rank, row in enumerate(channel_rows[:limit], 1):
                key = (row['start_char'], row['end_char'])
                target = fused.setdefault(key, dict(row, scores={}))
                target['scores'][channel] = 1 / (60 + rank)
        rows = sorted(fused.values(), key=lambda row: (
            -sum(row['scores'].values()), row['start_char'], row['id'],
        ))
        return self._results(
            scope, section_ids, query, rows, f'semantic:{query}', False,
            candidate_limit, page_size, warnings,
            truncated_channels=[channel for channel, rows_, limit in channels
                                if len(rows_) > limit],
        )

    @staticmethod
    def _normalized(value):
        return ' '.join(re.findall(r'[\wё]+', (value or '').casefold().replace('ё', 'е')))

    def find_entities(self, query=None, *, entity_types=None, limit=10):
        """Найти BookEntity по canonical name и aliases; entity_types фильтрует
        по типу (например ['location','institution'] для хронотопа). Пустой
        query с entity_types — перечислить все сущности этого типа без матчинга
        по имени."""
        if type(limit) is not int or not 1 <= limit <= 30:
            raise ValueError('limit должен быть от 1 до 30')
        if entity_types is not None and (not isinstance(entity_types, (list, tuple))
                                          or not entity_types or not all(isinstance(t, str) for t in entity_types)):
            raise ValueError('entity_types — непустой список строк')
        if query is None or query == '':
            if not entity_types:
                raise ValueError('Укажи query или entity_types')
            query = None
        else:
            query = self._query(query)
        catalog = self.repository.entity_catalog(self.book_id)
        if entity_types:
            catalog = [entity for entity in catalog if entity.get('entity_type') in entity_types]
        if query is None:
            rows = [{**dict(entity), 'score': None} for entity in catalog]
            rows.sort(key=lambda row: (-row['scene_count'], row['id']))
            return {'book_id': self.book_id, 'query': query, 'items': rows[:limit], 'ambiguous': False}
        needle = self._normalized(query)
        needle_tokens = set(needle.split())
        rows = []
        for entity in catalog:
            values = [entity.get('canonical_name', ''), *entity.get('aliases', [])]
            scores = []
            for value in values:
                normalized = self._normalized(value)
                tokens = set(normalized.split())
                overlap = len(needle_tokens & tokens) / max(1, len(needle_tokens | tokens))
                exact = 1.0 if normalized == needle else 0.0
                contains = 0.92 if needle and (needle in normalized or normalized in needle) else 0.0
                scores.append(max(exact, contains, overlap,
                                  SequenceMatcher(None, needle, normalized).ratio() * 0.8))
            score = max(scores, default=0)
            if score >= 0.35:
                rows.append({**dict(entity), 'score': round(score, 6)})
        rows.sort(key=lambda row: (-row['score'], -row['scene_count'], row['id']))
        return {'book_id': self.book_id, 'query': query, 'items': rows[:limit],
                'ambiguous': len(rows) > 1 and bool(rows[:2])
                and rows[0]['score'] - rows[1]['score'] < 0.08}

    def get_scenes_for_entities(self, entity_ids, *, mode='intersection',
                                section_ids=None, candidate_limit=200, page_size=10):
        """Scene union/intersection для явно выбранных BookEntity."""
        self._ids(entity_ids)
        if mode not in {'intersection', 'union'}:
            raise ValueError('mode должен быть intersection или union')
        scope = self._scope(section_ids, page_size, candidate_limit)
        found = self.repository.entity_ids(self.book_id, entity_ids)
        if set(found) != set(entity_ids):
            raise ValueError('Одна или несколько сущностей отсутствуют в этой книге')
        rows = self.repository.entity_scenes(
            self.book_id, list(dict.fromkeys(entity_ids)),
            'intersection' if mode == 'intersection' else 'union',
            scope[2], candidate_limit,
        )
        source = f'entities:{mode}:' + ','.join(sorted(entity_ids))
        return self._results(scope, section_ids, None, rows, source, True,
                             candidate_limit, page_size)

    def get_neighbors(self, scene_id, *, before=0, after=0, section_ids=None,
                      page_size=10):
        """Предыдущие/следующие сцены в reading order от одной anchor scene."""
        if (not isinstance(scene_id, str) or not scene_id or type(before) is not int
                or type(after) is not int or not 0 <= before <= 10
                or not 0 <= after <= 10 or before + after == 0):
            raise ValueError('Нужен scene_id; before/after 0..10 и хотя бы одно направление')
        scope = self._scope(section_ids, page_size, before + after)
        rows = self.repository.neighbors(
            self.book_id, scene_id, before, after, scope[2],
        )
        if rows is None:
            raise ValueError('Anchor scene отсутствует или устарела')
        return self._results(scope, section_ids, None, rows,
                             f'neighbors:{scene_id}:{before}:{after}', True,
                             before + after, page_size)

    def expand_graph(self, *, search_id=None, candidate_ids=None, scene_ids=None,
                     section_ids=None, hops=1, candidate_limit=200, page_size=10):
        """Явные начальные кандидаты ИЛИ сцены. hops=0: содержащие сцены, 1..3: соседи."""
        if type(hops) is not int or not 0 <= hops <= 3:
            raise ValueError('hops должен быть от 0 до 3')
        if (search_id is not None) == (scene_ids is not None):
            raise ValueError('Укажи search_id с candidate_ids либо scene_ids')
        if search_id is not None:
            if section_ids is not None:
                raise ValueError('Область наследуется от исходного поиска')
            state = self._state(search_id)
            self._ids(candidate_ids)
            rows = [r for r in state['items'] if r['id'] in candidate_ids]
            if len(rows) != len(set(candidate_ids)):
                raise ValueError('Кандидат не принадлежит исходному поиску')
            section_ids = state['section_ids']
            spans = [dict(start=r['start_char'], end=r['end_char']) for r in rows]
            selected_scenes = []
        else:
            if candidate_ids is not None:
                raise ValueError('Для candidate_ids нужен search_id')
            self._ids(scene_ids)
            spans, selected_scenes = [], scene_ids
        scope = self._scope(section_ids, page_size, candidate_limit)
        if search_id is not None:
            self._check(state)
        else:
            found = self.repository.scene_ids(self.book_id, scene_ids, scope[2])
            if set(found) != set(scene_ids):
                raise ValueError('Сцена отсутствует, устарела или находится вне области поиска')
        rows = self.repository.related_scenes(self.book_id, spans, selected_scenes, scope[2], hops, candidate_limit)
        # Graph-поиск от явных сцен работает даже без текстового индекса.
        return self._results(scope, section_ids, None, rows, f'graph:{uuid4().hex}', True,
                             candidate_limit, page_size)

    def expand_narrative(self, *, search_id=None, candidate_ids=None, scene_ids=None,
                        section_ids=None, channels=('discourse',), hops=1, levels_up=1,
                        candidate_limit=200, page_size=10):
        """Обход по нарратологически осмысленным каналам, а не только порядку чтения.

        discourse — соседи по NEXT_SCENE (как expand_graph); composition — сцены
        под тем же структурным предком на levels_up уровней выше (без LLM, через
        уже подтверждённое дерево разделов); character/chronotope — сцены с теми
        же person/location-сущностями, что в анкорных сценах (нужен построенный
        entity-граф, иначе канал не даёт кандидатов и не считается ошибкой);
        retelling/parallel/causal — чанки событий, связанных с анкорными через
        NARRATIVE_LINK (retells/parallels/causes, стадия 8+связи); soft_character —
        сцены с сущностями, которые book_resolution пометил как POSSIBLY_SAME с
        person-сущностями анкорных сцен (неуверенное совпадение, не merge).
        Найденное этими четырьмя каналами несёт человекочитаемый path — что
        именно нашло кандидата, см. agent/render.py.
        Каналы сводятся через RRF, как text/summary в search_semantic.
        """
        allowed = {'discourse', 'composition', 'character', 'chronotope', 'retelling',
                  'prefiguring', 'parallel', 'causal', 'soft_character'}
        LINK_TYPE_OF = dict(retelling='retells', prefiguring='prefigures', parallel='parallels',
                            causal='causes')
        channels = tuple(dict.fromkeys(channels or ()))
        if not channels or not set(channels) <= allowed:
            raise ValueError(f'channels — непустое подмножество {sorted(allowed)}')
        if type(hops) is not int or not 0 <= hops <= 3:
            raise ValueError('hops должен быть от 0 до 3')
        if type(levels_up) is not int or not 1 <= levels_up <= 5:
            raise ValueError('levels_up должен быть от 1 до 5')
        if (search_id is not None) == (scene_ids is not None):
            raise ValueError('Укажи search_id с candidate_ids либо scene_ids')
        if search_id is not None:
            if section_ids is not None:
                raise ValueError('Область наследуется от исходного поиска')
            state = self._state(search_id)
            self._ids(candidate_ids)
            rows = [r for r in state['items'] if r['id'] in candidate_ids]
            if len(rows) != len(set(candidate_ids)):
                raise ValueError('Кандидат не принадлежит исходному поиску')
            section_ids = state['section_ids']
            spans = [dict(start=r['start_char'], end=r['end_char']) for r in rows]
        else:
            if candidate_ids is not None:
                raise ValueError('Для candidate_ids нужен search_id')
            self._ids(scene_ids)
            spans = None
        scope = self._scope(section_ids, page_size, candidate_limit)
        book, sections, ranges = scope
        if search_id is not None:
            self._check(state)
            anchors = self.repository.scenes_in_ranges(self.book_id, spans, 200)
        else:
            anchors = self.repository.scene_ranges(self.book_id, list(dict.fromkeys(scene_ids)))
            if {row['id'] for row in anchors} != set(scene_ids):
                raise ValueError('Сцена отсутствует, устарела или находится вне области поиска')
            spans = [dict(start=row['start_char'], end=row['end_char']) for row in anchors]
        anchor_scenes = [row['id'] for row in anchors]

        fused = {}
        def add_channel(channel, channel_rows):
            # setdefault сохраняет поля первого канала, нашедшего id; path не
            # должен теряться, если тот первый канал (напр. discourse) его не нёс.
            for rank, row in enumerate(channel_rows[:candidate_limit], 1):
                target = fused.setdefault(row['id'], dict(row, scores={}))
                target['scores'][channel] = max(1 / (60 + rank), target['scores'].get(channel, 0))
                if row.get('path') and not target.get('path'):
                    target['path'] = row['path']

        if 'discourse' in channels:
            add_channel('discourse', self.repository.related_scenes(
                self.book_id, spans, anchor_scenes, ranges, hops, candidate_limit))
        if 'composition' in channels and anchors:
            ancestor_ids = {self._ancestor_id(sections, row['start_char'], row['end_char'], levels_up)
                            for row in anchors}
            ancestor_ids.discard(None)
            if ancestor_ids:
                composition_ranges = searchable_ranges(sections, sorted(ancestor_ids))
                add_channel('composition', self.repository.scenes_in_ranges(
                    self.book_id, composition_ranges, candidate_limit))
        for channel, types in (('character', ['person']),
                               ('chronotope', ['location', 'institution'])):
            if channel in channels and anchor_scenes:
                entity_ids = self.repository.entities_for_scenes(self.book_id, anchor_scenes, types)
                if entity_ids:
                    add_channel(channel, self.repository.entity_scenes(
                        self.book_id, entity_ids, 'union', ranges, candidate_limit))
        for channel, link_type in LINK_TYPE_OF.items():
            if channel in channels and (spans or anchor_scenes):
                linked = self.repository.linked_events(
                    self.book_id, spans or [], anchor_scenes, [link_type], ranges, candidate_limit)
                for row in linked:
                    row['path'] = _link_path(row, row['target_gist'])
                scene_linked = self.repository.linked_scenes(
                    self.book_id, spans or [], anchor_scenes, [link_type], ranges, candidate_limit)
                for row in scene_linked:
                    row['path'] = _link_path(row, row['target_title'], scenes=True)
                add_channel(channel, linked + scene_linked)
        if 'soft_character' in channels and anchor_scenes:
            person_ids = self.repository.entities_for_scenes(self.book_id, anchor_scenes, ['person'])
            possibly_same = self.repository.entities_possibly_same(self.book_id, person_ids) \
                if person_ids else []
            if possibly_same:
                soft_rows = self.repository.entity_scenes(
                    self.book_id, possibly_same, 'union', ranges, candidate_limit)
                for row in soft_rows:
                    row['path'] = 'вероятно то же лицо (неподтверждённое совпадение сущностей)'
                add_channel('soft_character', soft_rows)

        rows = sorted(fused.values(), key=lambda row: (
            -sum(row['scores'].values()), row['start_char'], row['id'],
        ))
        truncated = ['expand_narrative:discourse'] if (
            'discourse' in channels and len(rows) > candidate_limit) else []
        return self._results(scope, section_ids, None, rows,
                             f'narrative:{uuid4().hex}', True, candidate_limit, page_size,
                             truncated_channels=truncated)

    def related_fragments(self, search_id, candidate_id, *, similar=5, candidate_limit=50,
                          page_size=10):
        """Места, связанные с одним фрагментом поиска: события, связанные NARRATIVE_LINK
        с событиями внутри самого фрагмента (не всей его сцены), сцены, связанные
        SCENE_LINK с его сценой (обзор сюжета), и ближайшие по эмбеддингу чанки.
        У каждого кандидата path — как он найден, у связей — и причина связи.
        Область — как у исходного поиска."""
        if type(similar) is not int or not 0 <= similar <= 20:
            raise ValueError('similar должен быть от 0 до 20')
        state = self._state(search_id)
        item = next((r for r in state['items'] if r['id'] == candidate_id), None)
        if item is None:
            raise ValueError('Кандидат не принадлежит этому поиску')
        scope = self._scope(state['section_ids'], page_size, candidate_limit)
        _, _, ranges = scope
        span = dict(start=item['start_char'], end=item['end_char'])
        fused = {}

        def add(channel, rows):
            for rank, row in enumerate(rows, 1):
                target = fused.setdefault(row['id'], dict(row, scores={}))
                target['scores'][channel] = max(1 / (60 + rank), target['scores'].get(channel, 0))
                if row.get('path') and not target.get('path'):
                    target['path'] = row['path']

        linked = self.repository.linked_events(self.book_id, [span], [], list(LINK_LABELS),
                                               ranges, candidate_limit)
        for row in linked:
            row['path'] = _link_path(row, row['target_gist'], reason=True)
        add(f'related:links:{candidate_id}', linked)
        scene_linked = self.repository.linked_scenes(self.book_id, [span], [], list(LINK_LABELS),
                                                     ranges, candidate_limit)
        for row in scene_linked:
            row['path'] = _link_path(row, row['target_title'], scenes=True, reason=True)
        add(f'related:scenes:{candidate_id}', scene_linked)
        if similar and self.embedder is not None:
            rows = self.repository.similar_chunks(self.book_id, span, ranges, similar)
            for row in rows:
                row['path'] = 'похожий по смыслу фрагмент'
            add(f'related:similar:{candidate_id}', rows)
        rows = sorted(fused.values(), key=lambda row: (-sum(row['scores'].values()),
                                                       row['start_char'], row['id']))
        result = self._results(scope, state['section_ids'], None, rows,
                               f'related:{uuid4().hex}', False, candidate_limit, page_size)
        links = [dict(level='events', type=row['link_type'], basis=row.get('basis'),
                      reason=row.get('reason') or '', target=row['target_gist'],
                      anchor=row['anchor_gist'], start_char=row['start_char'],
                      end_char=row['end_char']) for row in linked]
        links += [dict(level='scenes', type=row['link_type'], basis=row.get('basis'),
                       reason=row.get('reason') or '', target=row['target_title'] or '',
                       anchor=row['anchor_title'] or '', start_char=row['start_char'],
                       end_char=row['end_char']) for row in scene_linked]
        return dict(result, links=links)

    @staticmethod
    def _ancestor_id(sections, scene_start, scene_end, levels_up):
        """ID предка на levels_up уровней выше самого узкого раздела, содержащего сцену."""
        by_id = {row['id']: row for row in sections}
        containing = [row for row in sections
                     if type(row.get('start_char')) is int and type(row.get('end_char')) is int
                     and row['start_char'] <= scene_start and row['end_char'] >= scene_end]
        if not containing:
            return None
        current = min(containing, key=lambda row: row['end_char'] - row['start_char'])
        for _ in range(levels_up):
            parent_id = current.get('parent_id')
            if not parent_id or parent_id not in by_id:
                break
            current = by_id[parent_id]
        return current['id']

    @staticmethod
    def _ids(ids):
        if not isinstance(ids, (list, tuple)) or not 1 <= len(ids) <= 200 or not all(isinstance(i, str) for i in ids):
            raise ValueError('Нужен список от 1 до 200 ID')

    def _results(self, scope, section_ids, query, rows, source, scenes, limit,
                 page_size, warnings=None, truncated_channels=None):
        book, sections, ranges = scope
        candidates = {}
        for rank, row in enumerate(rows[:limit], 1):
            for span in ranges:
                start, end = max(row['start_char'], span['start']), min(row['end_char'], span['end'])
                if start >= end:
                    continue
                key = f'{start}:{end}'
                row_scores = row.get('scores') or {source: 1 / (60 + rank)}
                item = candidates.setdefault(key, dict(id=key, start_char=start, end_char=end,
                    scores={}, scene_ids=[]))
                for channel, score in row_scores.items():
                    item['scores'][channel] = max(score, item['scores'].get(channel, 0))
                if scenes and row['id'] not in item['scene_ids']:
                    item['scene_ids'].append(row['id'])
                if row.get('path') and not item.get('path'):
                    item['path'] = row['path']
                anchor_start = row.get('anchor_start_char')
                anchor_end = row.get('anchor_end_char')
                if (type(anchor_start) is int and type(anchor_end) is int
                        and start <= anchor_start < anchor_end <= end):
                    anchor = dict(start_char=anchor_start, end_char=anchor_end)
                    if anchor not in item.setdefault('anchors', []):
                        item['anchors'].append(anchor)
        self._annotate_sections(candidates.values(), sections)
        return self._save(dict(scope=book, section_ids=deepcopy(section_ids), ranges=ranges,
                         items=list(candidates.values()), query=query, warnings=warnings or [],
                         truncated_channels=(truncated_channels if truncated_channels is not None
                                             else ([source] if len(rows) > limit else []))), page_size)

    @staticmethod
    def _annotate_sections(items, sections):
        for item in items:
            item['sections'] = [dict(id=s['id'], title=s['title'], role=s['role']) for s in sections
                                if s['end_char'] is not None and s['start_char'] < item['end_char']
                                and s['end_char'] > item['start_char']]

    def project_passages(self, search_id, *, query=None, page_size=10):
        """Свести кандидатов любого происхождения к поисковым отрывкам.

        Сцены (граф, описания сцен) и отрывки (лексика, текстовая семантика)
        становятся одной единицей оценки: каждый отрывок, пересекающий
        кандидата, наследует его оценки каналов (по каналу — максимум), так что
        RRF-ранжирование сохраняется, а оценивать и цитировать приходится
        короткий авторский абзац, а не сцену в несколько тысяч символов."""
        state = self._state(search_id)
        spans = [dict(id=item['id'], start=item['start_char'], end=item['end_char'],
                      scene_ids=item.get('scene_ids') or [])
                 for item in state['items']]
        by_id = {item['id']: item for item in state['items']}
        projected = {}
        for row in (self.repository.passages_overlapping(self.book_id, spans) if spans else []):
            source = by_id[row['span_id']]
            for span in state['ranges']:
                start, end = max(row['start_char'], span['start']), min(row['end_char'], span['end'])
                if start >= end:
                    continue
                key = f'{start}:{end}'
                item = projected.setdefault(key, dict(id=key, start_char=start, end_char=end,
                                                      scores={}, scene_ids=[]))
                for channel, score in source['scores'].items():
                    item['scores'][channel] = max(score, item['scores'].get(channel, 0))
                item['scene_ids'] = sorted(set(item['scene_ids']) | set(source['scene_ids']))
                if source.get('path') and not item.get('path'):
                    item['path'] = source['path']
                for anchor in source.get('anchors', []):
                    if start <= anchor['start_char'] < anchor['end_char'] <= end \
                            and anchor not in item.setdefault('anchors', []):
                        item['anchors'].append(anchor)
        _, sections, _ = self.repository.scope(self.book_id, state['section_ids'])
        self._annotate_sections(projected.values(), sections)
        state = dict(state, items=list(projected.values()), parent_search_ids=[search_id],
                     query=query if query is not None else state.get('query'))
        return self._save(state, page_size)

    def attach_plan(self, search_id, plan):
        """Сохранить план исследования (критерий, стратегии) вместе с поиском."""
        if not isinstance(plan, dict):
            raise ValueError('План должен быть объектом')
        state = self.sessions.load(search_id)
        state['plan'] = deepcopy(plan)
        self.sessions.save(search_id, state)

    def plan_of(self, search_id):
        return deepcopy(self.sessions.load(search_id).get('plan'))

    def latest_planned_search(self):
        """Последний актуальный поиск этого исследования, выполненный по плану."""
        for row in self.list_searches(limit=50):
            if row.get('has_plan') and row['current']:
                return row['search_id']
        return None

    def pending_candidates(self, search_id, limit):
        """Первые непроверенные кандидаты с текстом — порция для оценки (постранично)."""
        items, offset, total = [], 0, 0
        while len(items) < limit:
            page = self.continue_search(search_id, offset=offset, review_status='unreviewed',
                                        page_size=min(50, limit - len(items)))
            items += page['items']
            total = page['candidate_count']
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
        return items, total

    def context_of(self, search_id, candidate_ids):
        """Сцена-контейнер и хвост/голова соседних TextChunk через NEXT_CHUNK.

        Даёт стадии оценки контекст без похода к LLM: название и summary сцены
        кандидата и по 200 символов текста соседних чанков книги до/после.
        Кандидат вне готовой сцены (устаревший слой, разрыв нарезки) получает
        пустой контекст, а не ошибку."""
        state = self._state(search_id)
        self._ids(candidate_ids)
        by_id = {item['id']: item for item in state['items']}
        missing = [c for c in candidate_ids if c not in by_id]
        if missing:
            raise ValueError('Кандидат не принадлежит этому поиску')
        spans = [dict(id=c, start=by_id[c]['start_char'], end=by_id[c]['end_char'])
                 for c in dict.fromkeys(candidate_ids)]
        rows = {row['span_id']: row for row in self.repository.chunk_context(self.book_id, spans)}
        self._check(state)
        fields = ('scene_id', 'scene_title', 'scene_summary', 'prev_tail', 'next_head')
        empty = dict(scene_id=None, scene_title=None, scene_summary=None,
                     prev_tail='', next_head='')
        return {c: {field: rows.get(c, empty).get(field, empty[field]) for field in fields}
                for c in candidate_ids}

    def review_counts(self, search_id):
        state = self._state(search_id)
        counts = dict(relevant=0, uncertain=0, rejected=0, unreviewed=0)
        for item in state['items']:
            counts[item['review_status']] += 1
        return counts

    def apply_verdicts(self, search_id, verdicts):
        """Записать решения оценки одной транзакцией снимка.

        verdicts: [{candidate_id, status, reason, evidence}]. evidence —
        дословная выдержка, которой модель подтверждает решение; код находит её
        в тексте кандидата (без учёта регистра, ё/е и пробелов) и расширяет до
        границ предложения. Если выдержка не найдена, цитатой остаётся весь
        кандидат и это явно помечается. Возвращает подтверждённые цитаты для
        relevant/uncertain."""
        state = self._state(search_id)
        by_id = {item['id']: item for item in state['items']}
        rows = [by_id[v['candidate_id']] for v in verdicts if v['candidate_id'] in by_id]
        texts = {r['id']: r['text'] for r in self.repository.excerpts(
            self.book_id, rows, max((r['end_char'] - r['start_char'] for r in rows), default=1))}
        now = datetime.now(timezone.utc).isoformat()
        quotes = []
        for verdict in verdicts:
            item = by_id.get(verdict['candidate_id'])
            if item is None:
                continue
            status, reason = verdict['status'], verdict['reason'].strip()
            if status not in {'relevant', 'uncertain', 'rejected'}:
                raise ValueError('Статус оценки: relevant, uncertain, rejected')
            item.setdefault('review_history', []).append(dict(status=status, reason=reason, at=now))
            item.update(review_status=status, reason=reason, selected=False, viewed=True)
            if status == 'rejected':
                continue
            text = texts[item['id']]
            span = _evidence_span(text, verdict.get('evidence') or '')
            verified = span is not None
            start, end = (item['start_char'] + span[0], item['start_char'] + span[1]) if verified \
                else (item['start_char'], item['end_char'])
            selection = dict(start_char=start, end_char=end)
            item['selected_ranges'] = [selection]
            item.update(selected=True, evidence_verified=verified)
            quotes.append(dict(candidate_id=item['id'], status=status, reason=reason,
                               text=text[start - item['start_char']:end - item['start_char']],
                               start_char=start, end_char=end, verified=verified,
                               sections=deepcopy(item['sections']), book_id=self.book_id,
                               revision=state['scope']['revision'], path=item.get('path')))
        self._check(state)
        self.sessions.save(search_id, state, set(by_id))
        return quotes

    def merge_results(self, search_ids, *, page_size=10, query=None):
        """Объединяет ВСЕ сохранённые кандидаты поисков в одной области, без новых запросов поиска."""
        self._page_size(page_size)
        if not isinstance(search_ids, (list, tuple)) or not 1 <= len(search_ids) <= 20:
            raise ValueError('Укажи от 1 до 20 search_id')
        states = [self._state(s) for s in dict.fromkeys(search_ids)]
        first = states[0]
        if any(s['scope'] != first['scope'] or s['ranges'] != first['ranges'] for s in states):
            raise ValueError('Можно объединять только поиски одной версии и области книги')
        items = {}
        for state in states:
            for row in state['items']:
                item = items.setdefault(row['id'], deepcopy(row))
                for channel, score in row['scores'].items():
                    item['scores'][channel] = max(score, item['scores'].get(channel, 0))
                item['scene_ids'] = sorted(set(item['scene_ids']) | set(row['scene_ids']))
                if row.get('path') and not item.get('path'):
                    item['path'] = row['path']
                item['viewed'] = item.get('viewed', False) or row.get('viewed', False)
                item['context_read'] = item.get('context_read', False) or row.get('context_read', False)
                item['selected'] = item.get('selected', False) or row.get('selected', False)
                for field in ('selected_ranges', 'review_history'):
                    for entry in row.get(field, []):
                        if entry not in item.setdefault(field, []):
                            item[field].append(deepcopy(entry))
                left, right = item.get('review_status', 'unreviewed'), row.get('review_status', 'unreviewed')
                if left != right and right != 'unreviewed':
                    if left == 'unreviewed':
                        item.update(review_status=right, reason=row.get('reason', ''))
                    else:
                        item.update(review_status='uncertain', selected=False,
                                    reason='Решения исходных поисков различаются: '+item.get('reason', '')+'; '+row.get('reason', ''))
                if item.get('review_status') == 'rejected':
                    item['selected'] = False
        merged = dict(first, items=list(items.values()), query=query, parent_search_ids=list(search_ids),
                      warnings=list(dict.fromkeys(w for s in states for w in s['warnings'])),
                      truncated_channels=list(dict.fromkeys(c for s in states for c in s['truncated_channels'])))
        return self._save(merged, page_size)

    def _save(self, state, page_size):
        self._check(state)
        for item in state['items']:
            item['score'] = sum(item['scores'].values())
            item['sources'] = list(item['scores'])
        by_id = {item['id']: item for item in state['items']}
        order = _round_robin_order(state['items'])
        state['items'] = [by_id[item_id] for item_id in order]
        search_id = uuid4().hex
        state.pop('version', None)
        state['cursor'] = 0
        for item in state['items']:
            item.setdefault('viewed', False)
            item.setdefault('context_read', False)
            item.setdefault('review_status', 'unreviewed')
            item.setdefault('reason', '')
            item.setdefault('selected', False)
        self.sessions.save(search_id, state)
        return self.continue_search(search_id, page_size=page_size)

    @staticmethod
    def _page_size(value):
        if type(value) is not int or not 1 <= value <= 50:
            raise ValueError('page_size должен быть от 1 до 50')

    def _check(self, state):
        current, _, _ = self.repository.scope(self.book_id, state['section_ids'])
        if any(current[k] != state['scope'][k] for k in ('revision', 'generation')):
            raise ConflictError('Книга или индекс изменились; запусти поиск заново')

    def _state(self, search_id):
        state = self.sessions.load(search_id)
        self._check(state)
        return state

    def continue_search(self, search_id, *, offset=None, page_size=10, review_status=None):
        """Следующая страница сохранённых кандидатов; без новых embedding-вызовов."""
        self._page_size(page_size)
        if offset is not None and (type(offset) is not int or offset < 0):
            raise ValueError('offset должен быть неотрицательным целым')
        if review_status not in {None, 'unreviewed', 'relevant', 'rejected', 'uncertain'}:
            raise ValueError('Неизвестный статус проверки')
        state = self._state(search_id)
        start = state.get('cursor', 0) if offset is None and review_status is None else (offset or 0)
        positions = [i for i in range(start, len(state['items']))
                     if review_status is None or state['items'][i]['review_status'] == review_status]
        chosen = positions[:page_size]
        rows = [state['items'][i] for i in chosen]
        excerpts = {r['id']: r['text'] for r in self.repository.excerpts(self.book_id, rows, 1200)}
        for row in rows:
            row['viewed'] = True
        items = [dict({k: v for k, v in r.items() if k not in {'scores', 'review_history'}}, text=excerpts[r['id']], excerpt_end_char=r['start_char']+len(excerpts[r['id']]),
                      relevance='unverified') for r in rows]
        self._check(state)
        total = len(state['items'])
        stop = chosen[-1]+1 if chosen else total
        if review_status is None:
            state['cursor'] = max(state.get('cursor', 0), stop)
        self.sessions.save(search_id, state, {r['id'] for r in rows})
        return dict(book_id=self.book_id, search_id=search_id, revision=state['scope']['revision'],
                    query=state['query'], items=items, candidate_count=total,
                    next_offset=stop if len(positions) > len(chosen) else None,
                    remaining_unreviewed=sum(r['review_status']=='unreviewed' for r in state['items']),
                    truncated_channels=state['truncated_channels'],
                    warnings=state['warnings'], exhaustive=False)

    def read_context(self, search_id, candidate_id, *, before=400, after=400, offset=0, limit=6000):
        """Точная цитата с ограничением объёма и границами исходной области поиска."""
        if any(type(v) is not int or v < 0 for v in (before, after, offset)) or max(before, after) > 10000:
            raise ValueError('before/after: 0..10000; offset >= 0')
        state = self._state(search_id)
        item = next((r for r in state['items'] if r['id'] == candidate_id), None)
        if item is None:
            raise ValueError('Кандидат не принадлежит этому поиску')
        span = next(r for r in state['ranges'] if r['start'] <= item['start_char'] and r['end'] >= item['end_char'])
        start = max(span['start'], item['start_char']-before)
        end = min(span['end'], item['end_char']+after)
        result = self.books.read_text(self.book_id, start+offset, end, limit)
        self._check(state)
        item['context_read'] = True
        self.sessions.save(search_id, state, {candidate_id})
        return dict(result, candidate_id=candidate_id, revision=state['scope']['revision'],
                    next_offset=result['next_start']-start if result['next_start'] is not None else None)

    def list_searches(self, offset=0, limit=20):
        """История этого исследования, включая устаревшие поиски и счётчики решений."""
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('offset >= 0, limit 1..50')
        current, _, _ = self.repository.scope(self.book_id, None)
        return self.sessions.recent(offset, limit, current=current)

    def review_candidate(self, search_id, candidate_id, status, reason):
        """Явное решение по запросу; просмотр страницы сам по себе не означает проверку."""
        if status not in {'unreviewed', 'relevant', 'rejected', 'uncertain'}:
            raise ValueError('Статус: unreviewed, relevant, rejected, uncertain')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError('Укажи причину решения до 2000 символов')
        state = self._state(search_id)
        item = next((r for r in state['items'] if r['id'] == candidate_id), None)
        if item is None:
            raise ValueError('Кандидат не принадлежит поиску')
        item.setdefault('review_history', []).append(dict(status=status, reason=reason.strip(), at=datetime.now(timezone.utc).isoformat()))
        item.update(review_status=status, reason=reason.strip())
        if status != 'relevant':
            item['selected'] = False
        self.sessions.save(search_id, state, {candidate_id})
        return dict(search_id=search_id, candidate_id=candidate_id, status=status, reason=reason)

    def inspect_search(self, search_id, offset=0, limit=20):
        """История решений без исходного текста; доступна и для устаревшей версии книги."""
        self._page_size(limit)
        if type(offset) is not int or offset < 0:
            raise ValueError('offset >= 0')
        state = self.sessions.load(search_id)
        current, _, _ = self.repository.scope(self.book_id, None)
        valid = all(current[k] == state['scope'][k] for k in ('revision', 'generation'))
        fields = ('id', 'start_char', 'end_char', 'viewed', 'context_read', 'review_status',
                  'reason', 'selected', 'selected_ranges', 'review_history')
        return dict(search_id=search_id, query=state['query'], current=valid,
                    parent_search_ids=state.get('parent_search_ids', []),
                    cursor=state.get('cursor', 0), candidate_count=len(state['items']),
                    items=[{k: row[k] for k in fields if k in row} for row in state['items'][offset:offset+limit]],
                    next_offset=offset+limit if offset+limit < len(state['items']) else None)
