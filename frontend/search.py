"""Фасад над SearchService для вкладки «Находки».

Чтение сохранённых поисков, ручное решение исследователя по фрагменту
(SearchService.review_candidate) и экспорт. Выполнение новых поисков остаётся
диалоговым действием агента в чате — вкладка не дублирует эту логику.
"""
import csv
import io
import json
import re

from infrastructure.sqlite import get_search_sessions
from retrieval import SearchService
from retrieval.embeddings import configured_embedder
from structure.graph_db import get_repository


STATUS_LABELS = {
    'relevant': 'Подходит', 'rejected': 'Отклонён',
    'uncertain': 'Под вопросом', 'unreviewed': 'Не проверен',
}
STATUS_ORDER = ('relevant', 'uncertain', 'rejected', 'unreviewed')
MANUAL_REASON = 'Решение исследователя'

CANDIDATES_PAGE_SIZE = 20


_WORD_TARGETS = dict(text='в тексте', scenes='в описании сцены', context='в контексте отрывка')
_CHANNEL_LABELS = dict(
    discourse='соседняя сцена', composition='та же часть книги', character='те же персонажи',
    chronotope='то же место', soft_character='вероятно то же лицо',
    retelling='связь: пересказ', prefiguring='связь: предвестие', parallel='связь: параллель',
    causal='связь: причина', neighbors='соседняя сцена', graph='соседняя сцена',
)
HOW_FOUND_LIMIT = 3


def _channel_label(channel):
    """Человекочитаемое «как найдено» по имени канала оценки (см. SearchService)."""
    kind, _, rest = channel.partition(':')
    if kind == 'words':
        target, _, query = rest.partition(':')
        return f'слово «{query}» {_WORD_TARGETS.get(target, "")}'.strip()
    if kind == 'semantic':
        target, _, query = rest.partition(':')
        if target in ('text', 'summary'):
            return f'по смыслу{" сцены" if target == "summary" else ""}: «{query}»'
        return f'по смыслу: «{rest}»'
    if kind == 'entities':
        return 'присутствуют выбранные персонажи или места'
    if kind == 'related':
        return 'похожий по смыслу фрагмент' if rest.startswith('similar') else 'связано с фрагментом'
    return _CHANNEL_LABELS.get(kind)


def how_found(row):
    """Все пути, которыми фрагмент попал в выборку: сохранённый path (связи с
    причиной) и каналы с наибольшим вкладом, без повторов, не больше трёх."""
    labels = [row['path']] if row.get('path') else []
    scores = row.get('scores') or {}
    for channel in sorted(scores, key=lambda key: -scores[key]):
        label = _channel_label(channel)
        if label and label not in labels and not (
                label.startswith('связь:') and row.get('path')):
            labels.append(label)
    return labels[:HOW_FOUND_LIMIT]


def export_filename(query, extension):
    """Имя файла по запросу, а не по техническому id поиска."""
    slug = re.sub(r'[^\w]+', '-', (query or '').casefold()).strip('-')[:60] or 'поиск'
    return f'находки-{slug}.{extension}'


class Findings:
    def __init__(self, book_id, chat_id, repository=None, service=None, book_title=None):
        self.book_id = book_id
        self.book_title = book_title or book_id
        self.chat_id = chat_id or 'default'
        self.service = service or SearchService(
            book_id, repository=repository or get_repository(initialize=False),
            embedder=configured_embedder(), research_id=self.chat_id,
            sessions=get_search_sessions(book_id, self.chat_id),
        )

    def searches(self):
        """Итоговые поиски диалога — по одному на план; промежуточные поиски
        отдельных формулировок и их объединения не показываются."""
        return [
            dict(id=row['search_id'], query=row['query'] or '(объединённый поиск)',
                 candidates=str(row['candidate_count']), unreviewed=str(row['unreviewed']),
                 selected=str(row['selected']), rejected=str(row['rejected']),
                 current=str(bool(row['current'])).lower(),
                 updated_at=(row['updated_at'] or '')[:16].replace('T', ' '))
            for row in self.service.list_searches(limit=50) if row.get('has_plan')
        ][:20]

    @staticmethod
    def quote(row):
        """Выбранная оценкой цитата; без неё — весь отрывок."""
        ranges = row.get('selected_ranges') or []
        if not ranges:
            return row['text']
        start = ranges[0]['start_char'] - row['start_char']
        return row['text'][start:ranges[0]['end_char'] - row['start_char']]

    @classmethod
    def _item(cls, row):
        quote = cls.quote(row)
        sections = row.get('sections', [])
        return dict(
            id=row['id'], text=quote, context=row['text'] if quote != row['text'] else '',
            start_char=str(row['start_char']), end_char=str(row['end_char']),
            status=row['review_status'],
            status_label=STATUS_LABELS.get(row['review_status'], row['review_status']),
            reason=row.get('reason') or '', selected=str(bool(row.get('selected'))).lower(),
            sections=' / '.join(s['title'] or s['role'] for s in sections),
            # Самый узкий раздел фрагмента — для «Показать в тексте».
            section_id=sections[-1]['id'] if sections else '',
            path=row.get('path') or '',
            how_found='\n'.join(how_found(row)),
        )

    def candidates(self, search_id, offset=0, page_size=CANDIDATES_PAGE_SIZE, status=None):
        """Страница фрагментов поиска; status фильтрует по решению (None — все)."""
        if status is not None and status not in STATUS_LABELS:
            raise ValueError(f'status: одно из {sorted(STATUS_LABELS)}')
        page = self.service.continue_search(search_id, offset=offset, page_size=page_size,
                                            review_status=status)
        items = [self._item(row) for row in page['items']]
        next_offset = page['next_offset']
        query = page['query'] or '(объединённый поиск)'
        return items, next_offset, query

    def counts(self, search_id):
        return self.service.review_counts(search_id)

    def review(self, search_id, candidate_id, status, previous_reason=''):
        """Решение исследователя по фрагменту; прежняя причина модели остаётся в
        тексте, чтобы было видно, что именно переопределено."""
        if status not in STATUS_LABELS:
            raise ValueError(f'status: одно из {sorted(STATUS_LABELS)}')
        previous = (previous_reason or '').strip()
        if previous.startswith(MANUAL_REASON):
            previous = previous.partition('было: ')[2].rstrip(')')
        reason = f'{MANUAL_REASON} (было: {previous[:300]})' if previous else MANUAL_REASON
        return self.service.review_candidate(search_id, candidate_id, status, reason)

    def _all_candidates(self, search_id):
        items, offset, query = [], 0, None
        while True:
            page = self.service.continue_search(search_id, offset=offset, page_size=50)
            items.extend(page['items'])
            query = page['query'] or '(объединённый поиск)'
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
        return items, query

    @staticmethod
    def _exportable(items, include_all):
        if include_all:
            return items
        return [row for row in items
                if row['review_status'] == 'relevant' or row.get('selected')]

    def export_markdown(self, search_id, include_all=False):
        items, query = self._all_candidates(search_id)
        items = self._exportable(items, include_all)
        lines = [f'# Находки: {query}', '',
                 f'Книга: {self.book_title}. Найдено фрагментов: {len(items)}.', '']
        for row in items:
            sections = ' / '.join(s['title'] or s['role'] for s in row.get('sections', []))
            status = STATUS_LABELS.get(row['review_status'], row['review_status'])
            lines.append(f"## {sections or 'Без раздела'} · символы {row['start_char']}–{row['end_char']} · {status}")
            if row.get('reason'):
                lines.append(f"*{row['reason']}*")
            lines.append('')
            lines.append('> ' + self.quote(row).replace('\n', '\n> '))
            lines.append('')
        if not items:
            lines.append('Нет фрагментов, подходящих под текущий фильтр экспорта.')
        return '\n'.join(lines)

    def export_json(self, search_id, include_all=False):
        items, query = self._all_candidates(search_id)
        items = self._exportable(items, include_all)
        payload = dict(book_id=self.book_id, search_id=search_id, query=query, items=[
            dict(id=row['id'], start_char=row['start_char'], end_char=row['end_char'],
                 quote=self.quote(row), quote_range=(row.get('selected_ranges') or [None])[0],
                 context=row['text'], review_status=row['review_status'],
                 reason=row.get('reason') or '',
                 sections=[s['title'] or s['role'] for s in row.get('sections', [])])
            for row in items
        ])
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def export_csv(self, search_id, include_all=False):
        """Таблица для Excel/Google Sheets: по строке на фрагмент. BOM — чтобы Excel
        открыл UTF-8 с кириллицей без ручного выбора кодировки."""
        items, query = self._all_candidates(search_id)
        items = self._exportable(items, include_all)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(['№', 'Статус', 'Раздел', 'Цитата', 'Отрывок целиком', 'Почему',
                         'Как искала', 'Начало (символ)', 'Конец (символ)', 'ID', 'Запрос'])
        for index, row in enumerate(items, 1):
            writer.writerow([
                index, STATUS_LABELS.get(row['review_status'], row['review_status']),
                ' / '.join(s['title'] or s['role'] for s in row.get('sections', [])),
                self.quote(row), row['text'], row.get('reason') or '',
                '; '.join(how_found(row)), row['start_char'], row['end_char'], row['id'], query,
            ])
        return '\ufeff' + buffer.getvalue()
