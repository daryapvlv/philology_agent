"""Инструменты агента для работы с выборкой фрагментов текущего диалога.

Выборка — последний поиск диалога с сохранённым критерием (SearchService.attach_plan);
её же показывает вкладка «Находки». Поиск кандидатов идёт без LLM, проверка — judge.
"""
from typing import Literal

from langchain_core.tools import tool

from .judge import judge, judge_items
from .retrieve import SEMANTIC_DEPTH, expand_from, merge, retrieve

Status = Literal['relevant', 'uncertain', 'rejected', 'unreviewed']
Channel = Literal['discourse', 'composition', 'character', 'chronotope']
STATUS_LABELS = dict(relevant='подходит', uncertain='под вопросом', rejected='отклонён',
                     unreviewed='не проверен')
LINK_LABELS = dict(retells='пересказ того же события', prefigures='предвестие',
                   causes='связь по причине', parallels='параллель')
BASIS_LABELS = dict(explicit='прямо в тексте', inferred='выводится из текста',
                    suggestive='гипотеза для проверки')
MAX_QUERY_WORDS = 6
MAX_WORDS = 8
MAX_DESCRIPTIONS = 4
MODEL_TEXT_CHARS = 500


def _sections(row):
    return ' / '.join(s['title'] or s['role'] for s in row.get('sections') or [])


def _quote_of(item):
    """Цитата, выбранная проверкой; без неё — весь отрывок."""
    ranges = item.get('selected_ranges') or []
    if not ranges:
        return item['text'], item['start_char'], item['end_char']
    start, end = ranges[0]['start_char'], ranges[0]['end_char']
    offset = start - item['start_char']
    return item['text'][offset:offset + end - start], start, end


class SelectionTools:
    def __init__(self, search, client, config, usage, limits, *, section_ids,
                 progress=None, cache_dir=None):
        self.search = search
        self.client, self.config, self.usage = client, config, usage
        self.limits = limits
        self.section_ids = frozenset(section_ids)
        self.progress = progress
        self.cache_dir = cache_dir
        self.fragments = {}
        self._markers = {}
        self.notes = []

    # ── состояние выборки ──

    def current(self):
        search_id = self.search.latest_planned_search()
        if search_id is None:
            raise ValueError('В этом диалоге ещё нет выборки — начни с search_fragments')
        return search_id, self.search.plan_of(search_id) or {}

    def summary(self):
        search_id = self.search.latest_planned_search()
        if search_id is None:
            return None
        plan = self.search.plan_of(search_id) or {}
        return dict(criterion=plan.get('criterion', ''), scope=plan.get('scope') or [],
                    counts=self.search.review_counts(search_id))

    def _register(self, quote, *, must_show):
        candidate_id = quote['candidate_id']
        marker = self._markers.get(candidate_id)
        if marker is None:
            marker = f'F{len(self._markers) + 1}'
            self._markers[candidate_id] = marker
        previous = self.fragments.get(marker, {})
        self.fragments[marker] = dict(quote, must_show=must_show or previous.get('must_show', False))
        return marker

    def _resolve(self, fragment):
        by_marker = {marker: candidate for candidate, marker in self._markers.items()}
        return by_marker.get(fragment.strip().strip('[]'), fragment)

    def _for_model(self, quote, marker):
        text = quote['text']
        if len(text) > MODEL_TEXT_CHARS:
            text = text[:MODEL_TEXT_CHARS] + '…'
        return dict(marker=marker, id=quote['candidate_id'], status=quote['status'],
                    section=_sections(quote), text=text, reason=quote.get('reason', ''))

    def _item_quote(self, item):
        text, start, end = _quote_of(item)
        return dict(candidate_id=item['id'], status=item['review_status'],
                    reason=item.get('reason') or '', text=text, start_char=start,
                    end_char=end, verified=item.get('evidence_verified'),
                    sections=item.get('sections') or [], path=item.get('path'))

    # ── проверка и отчёт ──

    def _checked(self, action, search_id, criterion, report, *, warnings=(), assumptions=()):
        quotes = sorted(report.quotes, key=lambda q: (q['status'] != 'relevant', q['start_char']))
        fragments = [self._for_model(q, self._register(q, must_show=True)) for q in quotes]
        counts = self.search.review_counts(search_id)
        line = (f'{action}: проверено {report.judged} из {report.total} кандидатов, в выборке '
                f'подходят {counts["relevant"]}, под вопросом {counts["uncertain"]}, '
                f'отклонено {counts["rejected"]}')
        if counts['unreviewed']:
            line += f', не проверено {counts["unreviewed"]} (можно попросить «найди ещё»)'
        self.notes.append(line + '.')
        self.notes += [f'Допущение: {note}.' for note in assumptions]
        self.notes += [f'Предупреждение: {note}.' for note in warnings]
        self.notes += [f'Сбой проверки: {note}.' for note in report.failures]
        if report.exhausted:
            self.notes.append('Бюджет проверки этого ответа исчерпан.')
        return dict(criterion=criterion, checked_now=report.judged, counts=counts,
                    fragments=fragments, rejected_reasons=report.rejected_reasons[:5],
                    warnings=list(warnings), assumptions=list(assumptions),
                    failures=report.failures, budget_exhausted=report.exhausted)

    def _judge_next(self, search_id, criterion):
        return judge(self.client, self.config, self.search, search_id, criterion,
                     limit=self.limits.judge_limit, batch_size=self.limits.judge_batch,
                     cache_dir=self.cache_dir, usage=self.usage, progress=self.progress)

    def _status_items(self, search_id, statuses, limit):
        items = []
        for status in statuses:
            offset = 0
            while len(items) < limit:
                page = self.search.continue_search(search_id, offset=offset, page_size=50,
                                                   review_status=status)
                items += page['items']
                if page['next_offset'] is None:
                    break
                offset = page['next_offset']
        return items[:limit]

    # ── проверка аргументов ──

    @staticmethod
    def _criterion(value):
        if not value.strip() or len(value) > 500:
            raise ValueError('criterion: непустое проверяемое условие до 500 символов')
        return value.strip()

    @staticmethod
    def _words(values):
        words = list(dict.fromkeys(q.strip() for q in values if q.strip()))
        if len(words) > MAX_WORDS:
            raise ValueError(f'words: не больше {MAX_WORDS} формулировок')
        long = [q for q in words if len(q.split()) > MAX_QUERY_WORDS or len(q) > 80]
        if long:
            raise ValueError(f'words: до {MAX_QUERY_WORDS} слов в формулировке; раздели на '
                             f'отдельные точные слова, а ситуацию опиши в descriptions: {long}')
        return words

    @staticmethod
    def _descriptions(values):
        descriptions = list(dict.fromkeys(d.strip() for d in values if d.strip()))
        if len(descriptions) > MAX_DESCRIPTIONS:
            raise ValueError(f'descriptions: не больше {MAX_DESCRIPTIONS} описаний')
        bad = [d for d in descriptions if len(d.split()) < 3 or len(d) > 200]
        if bad:
            raise ValueError('descriptions: одно предложение от 3 слов до 200 символов, '
                             f'описывающее ситуацию: {bad}')
        return descriptions

    def _scope(self, values):
        unknown = [s for s in values if s not in self.section_ids]
        if unknown:
            raise ValueError(f'scope: таких id нет в оглавлении: {unknown}')
        return list(values)

    def _require_index(self):
        status = self.search.index_status()
        if not status['generation'] or not status['current']:
            raise ValueError('Поисковый индекс книги не готов. Пользователю нужно подготовить '
                             'книгу во вкладке «Подготовка».')
        return status

    # ── инструменты ──

    def definitions(self):
        @tool
        def search_fragments(criterion: str, words: list[str] = [], descriptions: list[str] = [],
                             entities: list[str] = [],
                             entity_mode: Literal['intersection', 'union'] = 'intersection',
                             scope: list[str] = [], expand: list[Channel] = []) -> dict:
            """Новая выборка: найти кандидатов по словам (все вхождения), описаниям ситуации
            (ближайшие по смыслу) и/или сущностям и проверить их по criterion. Возвращает
            проверенные фрагменты с маркерами для ответа."""
            criterion = self._criterion(criterion)
            words, descriptions = self._words(words), self._descriptions(descriptions)
            entities = [e.strip() for e in entities if e.strip()][:4]
            if not words and not descriptions and not entities:
                raise ValueError('Нужны words, descriptions или entities')
            status = self._require_index()
            if entities and not status['entity_graph_ready_scenes']:
                raise ValueError('Граф сущностей книги не построен — ищи по words и descriptions')
            scope = self._scope(scope)
            if self.progress:
                self.progress('Ищу кандидатов…')
            found = retrieve(self.search, criterion=criterion, words=words,
                             descriptions=descriptions, entities=entities,
                             entity_mode=entity_mode, scope=scope, expand=expand,
                             candidate_limit=self.limits.candidate_limit)
            described = ', '.join(f'«{q}»' for q in words + entities)
            if descriptions:
                described += ('; ' if described else '') + f'описаний по смыслу: {len(descriptions)}'
            if found.search_id is None:
                self.notes.append(f'Поиск ({described}): кандидатов не найдено.')
                return dict(criterion=criterion, candidates=0, fragments=[],
                            warnings=found.warnings, assumptions=found.assumptions)
            self.search.attach_plan(found.search_id, dict(
                criterion=criterion, words=words, descriptions=descriptions, entities=entities,
                entity_mode=entity_mode, scope=scope, expand=list(expand),
                semantic_depth=SEMANTIC_DEPTH, assumptions=found.assumptions))
            report = self._judge_next(found.search_id, criterion)
            return self._checked(f'Поиск ({described})', found.search_id, criterion, report,
                                 warnings=found.warnings, assumptions=found.assumptions)

        @tool
        def expand_selection(words: list[str] = [], descriptions: list[str] = [],
                             channels: list[Channel] = []) -> dict:
            """Расширить текущую выборку: новые слова, новые описания и/или обход от
            подходящих фрагментов по каналам. Без аргументов («найди ещё») — углубить
            смысловой поиск по описаниям выборки и проверить непроверенное. Критерий
            выборки не меняется."""
            search_id, plan = self.current()
            criterion = plan.get('criterion', '')
            words, descriptions = self._words(words), self._descriptions(descriptions)
            scope = plan.get('scope') or []
            old_descriptions = plan.get('descriptions') or []
            depth = plan.get('semantic_depth') or SEMANTIC_DEPTH
            deepen = not (words or descriptions or channels)
            if deepen:
                depth = min(depth * 2, self.limits.candidate_limit)
            warnings, assumptions, new = [], [], []
            search_descriptions = descriptions + (old_descriptions if deepen else [])
            if words or search_descriptions or channels:
                self._require_index()
                if self.progress:
                    self.progress('Ищу новых кандидатов…')
            if words or search_descriptions:
                found = retrieve(self.search, criterion=criterion, words=words,
                                 descriptions=search_descriptions, scope=scope,
                                 candidate_limit=self.limits.candidate_limit,
                                 semantic_depth=depth)
                warnings += found.warnings
                assumptions += found.assumptions
                if found.search_id:
                    new.append(found.search_id)
            if channels:
                expanded = expand_from(self.search, search_id, channels,
                                       review_status='relevant',
                                       limit=self.limits.candidate_limit)
                if expanded is None:
                    warnings.append('обход: в выборке нет подходящих фрагментов, от которых идти')
                else:
                    new.append(self.search.project_passages(
                        expanded, query=criterion, page_size=1)['search_id'])
            target = search_id
            if new:
                target = merge(self.search, [search_id, *new], query=criterion)
                old_words = plan.get('words') or plan.get('queries') or []
                self.search.attach_plan(target, dict(
                    plan, words=list(dict.fromkeys([*old_words, *words])),
                    descriptions=list(dict.fromkeys([*old_descriptions, *descriptions])),
                    expand=list(dict.fromkeys([*(plan.get('expand') or []), *channels])),
                    semantic_depth=depth))
            elif not deepen:
                self.notes.append('Расширение не дало новых кандидатов.')
            report = self._judge_next(target, criterion)
            parts = [f'слова {", ".join(f"«{q}»" for q in words)}'] if words else []
            parts += [f'новых описаний: {len(descriptions)}'] if descriptions else []
            parts += [f'обход: {", ".join(channels)}'] if channels else []
            action = ('Расширение (' + '; '.join(parts) + ')' if parts
                      else f'Углублённый смысловой поиск (глубина {depth})' if old_descriptions
                      else 'Следующая порция')
            return self._checked(action, target, criterion, report,
                                 warnings=warnings, assumptions=assumptions)

        @tool
        def related_fragments(fragment: str, similar: int = 5) -> dict:
            """Места книги, связанные с фрагментом выборки (маркер F… или id): пересказы
            того же события, предвестия, причинные связи и параллели — его событий и его
            сцены в целом, плюс ближайшие по смыслу отрывки. Найденное добавляется в
            выборку и проверяется её критерием; связи возвращаются с объяснениями и
            основанием (basis: прямо в тексте / выводится / гипотеза) — даже если отрывок
            критерию не подошёл."""
            search_id, plan = self.current()
            criterion = plan.get('criterion', '')
            if self.progress:
                self.progress('Ищу связанные места…')
            related = self.search.related_fragments(search_id, self._resolve(fragment),
                                                    similar=max(0, min(similar, 10)),
                                                    page_size=1)
            target = search_id
            if related['candidate_count']:
                projected = self.search.project_passages(related['search_id'], query=criterion,
                                                         page_size=1)
                target = merge(self.search, [search_id, projected['search_id']], query=criterion)
                self.search.attach_plan(target, plan)
            report = self._judge_next(target, criterion)
            links = [dict(level='события' if link['level'] == 'events' else 'сцены',
                          type=LINK_LABELS[link['type']], reason=link['reason'],
                          basis=BASIS_LABELS.get(link.get('basis'), 'не указано'),
                          this=link['anchor'], other=link['target'])
                     for link in related['links']]
            result = self._checked(f'Связанные места (связей: {len(links)})', target,
                                   criterion, report)
            return dict(result, links=links)

        @tool
        def filter_selection(criterion: str, include_uncertain: bool = True) -> dict:
            """Сузить выборку: перепроверить подходящие (и, по умолчанию, спорные)
            фрагменты по новому, более строгому criterion — его нужно сформулировать
            целиком. Не прошедшие проверку получают статус «отклонён»."""
            search_id, plan = self.current()
            criterion = self._criterion(criterion)
            statuses = ['relevant', 'uncertain'] if include_uncertain else ['relevant']
            items = self._status_items(search_id, statuses, self.limits.filter_limit)
            report = judge_items(self.client, self.config, self.search, search_id, criterion,
                                 items, batch_size=self.limits.judge_batch,
                                 cache_dir=self.cache_dir, usage=self.usage,
                                 progress=self.progress)
            history = [*(plan.get('previous_criteria') or []), plan.get('criterion', '')]
            self.search.attach_plan(search_id, dict(plan, criterion=criterion,
                                                    previous_criteria=history))
            return self._checked('Уточнение выборки', search_id, criterion, report)

        @tool
        def set_fragment_status(fragments: list[str], status: Status, reason: str) -> dict:
            """Явное решение пользователя по фрагментам выборки (маркеры F… этого ответа
            или id из show_selection): например, убрать или вернуть фрагмент."""
            search_id, _ = self.current()
            if not reason.strip():
                raise ValueError('reason: укажи, почему меняется статус')
            changed = []
            for fragment in fragments[:50]:
                self.search.review_candidate(search_id, self._resolve(fragment), status,
                                             f'По просьбе исследователя: {reason.strip()}')
                changed.append(fragment)
            self.notes.append(f'Статус «{STATUS_LABELS[status]}» выставлен фрагментам: '
                              f'{len(changed)}.')
            return dict(changed=changed, status=status,
                        counts=self.search.review_counts(search_id))

        @tool
        def show_selection(status: Status | None = None, offset: int = 0,
                           limit: int = 20) -> dict:
            """Фрагменты текущей выборки с маркерами и id, опционально одного статуса."""
            search_id, plan = self.current()
            page = self.search.continue_search(search_id, offset=offset,
                                               page_size=max(1, min(limit, 30)),
                                               review_status=status)
            fragments = []
            for item in page['items']:
                quote = self._item_quote(item)
                fragments.append(self._for_model(quote, self._register(quote, must_show=False)))
            return dict(criterion=plan.get('criterion', ''),
                        counts=self.search.review_counts(search_id),
                        fragments=fragments, next_offset=page['next_offset'])

        @tool
        def read_around(fragment: str, before: int = 600, after: int = 600) -> dict:
            """Текст книги вокруг фрагмента выборки (маркер F… или id)."""
            search_id, _ = self.current()
            return self.search.read_context(search_id, self._resolve(fragment),
                                            before=max(0, min(before, 3000)),
                                            after=max(0, min(after, 3000)), limit=6000)

        functions = [search_fragments, expand_selection, related_fragments, filter_selection,
                     set_fragment_status, show_selection, read_around]
        return {function.name: function for function in functions}


class BookTools:
    def __init__(self, book):
        self.book = book

    def definitions(self):
        @tool
        def read_section(section_id: str, offset: int = 0, limit: int = 4000) -> dict:
            """Прочитать часть раздела книги по id из оглавления, до 6000 символов."""
            return self.book.read_section(section_id, offset=offset,
                                          limit=max(1, min(limit, 6000)))

        @tool
        def get_scenes(chapter_id: str, offset: int = 0) -> dict:
            """Сцены главы: границы, названия и краткие описания."""
            return self.book.scenes(chapter_id, offset=offset, limit=10)

        return {function.name: function for function in (read_section, get_scenes)}
