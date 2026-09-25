"""Модель книги для UI над BookService."""
import json

from entities import EntityService
from narrative import NarrativeService, narrative_layer_enabled
from structure.application import BookService
from structure.rules import HEADING_ROLES, apply_human_sections
from structure.workflows.edit_sections import add_section, edit_section, split_section


ROLE_LABELS = {
    'collection': 'Сборник', 'work': 'Произведение', 'volume': 'Том',
    'part': 'Часть', 'chapter': 'Глава', 'section': 'Раздел',
    'paragraph': 'Параграф', 'preface': 'Предисловие', 'prologue': 'Пролог',
    'epilogue': 'Эпилог', 'appendix': 'Приложение', 'act': 'Действие',
    'picture': 'Картина', 'dramatic_scene': 'Явление',
    'dramatis_personae': 'Действующие лица', 'other': 'Раздел',
}


class Book:
    def __init__(self, book_id, repository=None):
        self.book_id = book_id
        self.service = BookService(book_id, repository)
        self.reload()

    def _merges(self):
        return {m["start"]: m["end"] for m in self.result.get("element_merges", [])}

    def _role_for(self, position, body_start, epigraphs, notes=()):
        """Роль одного элемента: opening / body / epigraph / attribution."""
        role = 'body' if body_start is None or position >= body_start else 'opening'
        for block in epigraphs:
            if block['start'] <= position < block['end']:
                attribution = block.get('attribution_start')
                role = ('attribution' if isinstance(attribution, int)
                        and position >= attribution else 'epigraph')
                break
        if any(block['start'] <= position < block['end'] for block in notes):
            role = 'footnotes'
        return role

    def reload(self):
        self.result, self.elements = self.service.snapshot()
        self.title = self.result['book_title']
        self._revision = self.result['revision']

    @property
    def pending_review(self):
        """Подтверждение произведений и глав; сцены его не требуют."""
        return self.result.get('pending_review', True)

    @property
    def can_undo_structure(self):
        return bool(self.result.get('structure_history'))

    @property
    def undo_structure_label(self):
        history = self.result.get('structure_history', [])
        return str(history[-1].get('label') or 'Последнее изменение') if history else ''

    @property
    def reviewed_elements(self):
        return self.result.get('reviewed_elements', 0)

    @property
    def total_elements(self):
        return len(self.elements)

    @property
    def issues(self):
        return self.result.get('issues', [])

    @property
    def structure_status(self):
        return self.result.get('section_analysis_status', 'needs_review')

    @property
    def review_items(self):
        return self.service.review_items(self.result)

    def outline(self):
        return [dict(
                    index=str(i),
                    label=s.get('title') or ROLE_LABELS.get(s['role'], 'Раздел'),
                    role=s['role'],
                    indent=f"{10 + min(s['level'] - 1, 6) * 14}px",
                )
                for i, s in enumerate(self.result['sections']) if s['role'] in HEADING_ROLES]

    def chapter_outline(self):
        """Читаемые разделы для оглавления, без корней-контейнеров книги."""
        sections = self.result['sections']
        chapter_roles = {
            'chapter', 'preface', 'prologue', 'epilogue', 'appendix',
            'act', 'picture', 'dramatic_scene',
        }
        chapters = []
        for index, section in enumerate(sections):
            if section.get('role') not in chapter_roles:
                continue
            chapters.append(dict(
                index=str(index),
                label=section.get('title') or ROLE_LABELS.get(section['role'], 'Глава'),
                role=section['role'],
                indent="8px",
                scene_status=self._scene_status(section),
            ))
        if chapters:
            return chapters

        # Старые импорты иногда используют section/paragraph вместо chapter.
        # В таком случае показываем все листовые читаемые разделы.
        containers = {'collection', 'work', 'volume', 'part'}
        readable = []
        for index, section in enumerate(sections):
            if section.get('role') in containers:
                continue
            owns_text = (
                isinstance(section.get('body_start'), int)
                and (section.get('body_end') is None
                     or section['body_start'] < section['body_end'])
            )
            is_leaf = not any(child.get('parent_id') == section['id'] for child in sections)
            if owns_text or is_leaf:
                readable.append(dict(
                    index=str(index),
                    label=section.get('title') or ROLE_LABELS.get(section['role'], 'Глава'),
                    role=section['role'],
                    indent="8px",
                    scene_status=self._scene_status(section),
                ))
        return readable

    def _scene_status(self, section):
        """Статус слоя сцен раздела — для отметки в списке глав, без запросов в БД."""
        return self.result.get('scenes', {}).get(section['id'], {}).get('status', 'not_started')

    def chapter_titles(self):
        """id главы → название: интерфейс показывает названия, а не технические id."""
        return {section['id']: section.get('title') or ROLE_LABELS.get(section['role'], 'Глава')
                for section in self.result['sections']}

    def section_index(self, section_id):
        """Индекс раздела по id (для «Показать в тексте» из находок) или None."""
        return next((str(i) for i, s in enumerate(self.result['sections'])
                     if s['id'] == section_id), None)

    def section(self, index):
        return self.result['sections'][int(index)]

    def element_preview(self, position):
        try:
            position = int(position)
        except (TypeError, ValueError):
            return "Введите номер блока"
        if position == len(self.elements):
            return f"{position} · конец книги"
        if not 0 <= position < len(self.elements):
            return "Такого блока нет"
        text = " ".join(self.elements[position].get("text", "").split())
        if len(text) > 110:
            text = text[:107].rstrip() + "…"
        return f"{position} · {text or 'пустой блок'}"

    def epigraphs(self, index):
        section = self.section(index)
        items = []
        for block in sorted(self.result.get('blocks', []), key=lambda item: item['start']):
            if block.get('section_id') != section['id'] or block.get('role') != 'epigraph':
                continue
            attribution = block.get('attribution_start')
            quote_end = attribution if attribution is not None else block['end']
            quote = '\n'.join(self.elements[i]['text'] for i in range(block['start'], quote_end)
                              if self.elements[i]['text'].strip())
            source = ('\n'.join(self.elements[i]['text'] for i in range(attribution, block['end'])
                                 if self.elements[i]['text'].strip())
                      if attribution is not None else '')
            items.append({'id': block['id'], 'quote': quote, 'attribution': source})
        return items

    def text(self, index, show_numbers=False):
        section = self.section(index)
        has_children = any(item.get('parent_id') == section['id']
                           for item in self.result['sections'])
        owns_text = (type(section.get('body_start')) is int
                     and type(section.get('body_end')) is int
                     and section['body_start'] < section['body_end'])
        if (has_children and not owns_text
                and section['role'] in {'collection', 'work', 'volume', 'part', 'act'}):
            return 'Выберите главу в оглавлении, чтобы прочитать её текст.'
        start = section.get('body_start')
        if start is None:
            start = section['start'] + (section.get('title_position') == section['start'])
        end = section.get('body_end') or section['end']
        stop = end if end is not None else min(start+5, len(self.elements))
        prefix = 'Конец не определён; показаны ближайшие элементы.\n\n' if end is None else ''
        merges = self._merges()
        parts = []
        position = start
        while position < stop:
            group_end = min(merges.get(position, position + 1), stop)
            group_text = '\n'.join(
                self.elements[i]['text'] for i in range(position, group_end)
                if self.elements[i]['text'].strip()
            )
            if group_text.strip():
                marker = f'[{position}] ' if show_numbers else ''
                parts.append(marker + group_text)
            position = group_end
        return prefix + '\n\n'.join(parts)

    def selectable_text(self, index):
        section = self.section(index)
        start = section["start"] + (section.get("title_position") == section["start"])
        end = section.get("body_end") or section.get("end") or min(start + 30, len(self.elements))
        body_start = section.get("body_start")
        epigraphs = [
            block for block in self.result.get('blocks', [])
            if block.get('section_id') == section['id'] and block.get('role') == 'epigraph'
        ]
        notes = [
            block for block in self.result.get('blocks', [])
            if block.get('section_id') == section['id'] and block.get('role') == 'footnotes'
        ]
        merges = self._merges()
        rows = []
        previous_role = None
        position = start
        while position < end:
            group_end = min(merges.get(position, position + 1), end)
            group_text = "\n".join(
                self.elements[i]["text"] for i in range(position, group_end)
                if self.elements[i]["text"].strip()
            )
            if group_text.strip():
                role = self._role_for(position, body_start, epigraphs, notes)
                group_start = role != previous_role
                role_meta = {
                    'epigraph': 'ЭПИГРАФ',
                    'attribution': 'СНОСКА / ИСТОЧНИК',
                    'body': 'ОСНОВНОЙ ТЕКСТ',
                    'opening': 'ЦИТАТА / ВЫДЕЛЕНИЕ',
                    'footnotes': 'ПРИМЕЧАНИЕ',
                }[role]
                rows.append({
                    "position": str(position),
                    "end_position": str(group_end),
                    "length": str(len(group_text)),
                    "text": group_text,
                    "role": role,
                    "inset": "8%" if role in {"epigraph", "attribution"} else "0",
                    "marker": (({
                        'epigraph': 'ЭПИГРАФ', 'attribution': 'ИСТОЧНИК',
                        'body': 'ТЕКСТ', 'opening': 'ВСТУПЛЕНИЕ',
                        'footnotes': 'ПРИМЕЧАНИЕ',
                    }[role] + ' · ') if role != previous_role else '') + str(position),
                    "group_start": str(group_start).lower(),
                    "role_label": role_meta,
                    "body_boundary": str(position == body_start and body_start > start).lower(),
                })
                previous_role = role
            position = group_end
        return rows

    def save(self, history_label=None):
        try:
            self.service.save(
                self.result, self._revision, history_label=history_label,
            )
        finally:
            self.reload()

    def edit(self, index, values):
        self.result, selected = edit_section(self.result, index, values, elements=self.elements)
        self.save('Изменение раздела')
        return int(selected)

    def add_section(self, values):
        self.result, selected = add_section(self.result, values, elements=self.elements)
        self.save('Создание раздела')
        return int(selected)

    def split(self, index, boundaries):
        self.result, selected = split_section(self.result, self.elements, index, boundaries)
        self.save('Разделение раздела')
        return int(selected)

    def scene_layer(self, index):
        return self.result.get('scenes', {}).get(self.section(index)['id'], {'status': 'not_started', 'items': []})

    def scene_previews(self, index):
        layer = self.scene_layer(index)
        if layer['status'] == 'stale':
            return []
        text = '\n'.join(e['text'] for e in self.elements)
        return [dict(id=s['id'], title=s['title'], summary=s['summary'],
                     start=str(s['start_char']), end=str(s['end_char']),
                     note='Описание нужно уточнить после изменения границ' if s.get('description_stale') else '',
                     text=s.get('text', text[s['start_char']:s['end_char']]))
                for s in layer['items']]

    def build_scenes(self, index, client, *, config=None, force=False,
                     replace_human=False, embedder=None):
        try:
            self.service.build_scenes(self.section(index)['id'], client, config=config,
                                      force=force, replace_human=replace_human,
                                      embedder=embedder)
        finally:
            self.reload()

    def chapter_entities(self, index):
        service = EntityService(self.book_id, self.service.repository)
        return service.chapter_entities(self.section(index)['id'])

    def analyze_chapter_entities(self, index, client, *, config=None):
        layer = self.scene_layer(index)
        if layer.get('status') != 'ready' or not layer.get('items'):
            raise ValueError('Сначала постройте актуальные сцены выбранной главы')
        service = EntityService(self.book_id, self.service.repository)
        for scene in layer['items']:
            service.extract_scene_mentions(scene['id'], client, config=config)
        return service.resolve_chapter(
            self.section(index)['id'], client, config=config,
        )

    def book_entities(self):
        return EntityService(self.book_id, self.service.repository).book_entities()

    def preparation_status(self, indexes):
        chapter_ids = [self.section(index)['id'] for index in indexes]
        status = EntityService(
            self.book_id, self.service.repository,
        ).preparation_status(chapter_ids)
        if narrative_layer_enabled():
            status.update(NarrativeService(
                self.book_id, self.service.repository,
            ).preparation_status(chapter_ids))
        return status

    def resolve_book_entities(self, client, *, config=None):
        return EntityService(
            self.book_id, self.service.repository,
        ).resolve_book(client, config=config, rebuild=True)

    def reconcile_book_entities(self, client, *, config=None):
        return EntityService(
            self.book_id, self.service.repository,
        ).reconcile_book(client, config=config)

    def merge_elements(self, start, end):
        try:
            self.service.merge_elements(start, end, self._revision)
        finally:
            self.reload()

    def unmerge_at(self, position):
        try:
            self.service.unmerge_at(position, self._revision)
        finally:
            self.reload()

    def update_opening(self, index, operation, start=None, end=None):
        try:
            self.service.update_opening(
                self.section(index)['id'], operation, start, end, self._revision,
            )
        finally:
            self.reload()

    def edit_scene(self, index, scene_id, operation, **changes):
        try:
            self.service.update_scene(self.section(index)['id'], scene_id, operation,
                                      self._revision, **changes)
        finally:
            self.reload()

    def approve(self):
        if not self.pending_review:
            return
        self.result['pending_review'] = False
        self.save()

    def undo_structure(self):
        try:
            self.service.undo_structure(self._revision)
        finally:
            self.reload()

    def export(self):
        result, _ = self.service.snapshot()
        if result.get('pending_review', True):
            raise ValueError('Сначала подтвердите текущую структуру')
        return json.dumps(result, ensure_ascii=False, indent=2)

    def restore_original(self):
        sections = self.service.repository.original_sections(self.book_id)
        self.result = apply_human_sections(self.result, self.elements, sections)
        self.save('Восстановление исходной структуры')
