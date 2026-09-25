"""Интерфейс книги для UI и будущих инструментов LangGraph.

book_id задаётся приложением при создании сервиса, не выбирается моделью.
Чтение — Neo4j. Правки — чистые функции и транзакционная запись с expected_revision.
Построение сцен вызывает LLM вне транзакций; правки структуры атомарны.
"""
from copy import deepcopy
import re
from threading import Lock
from uuid import NAMESPACE_URL, uuid5

from ..graph_db.repository import ConflictError, get_repository
from ..rules import apply_human_sections, has_scene_text, invalidate_scenes
from ..workflows.build_scenes import build_scenes
from ..workflows.edit_scenes import edit_scenes


STRUCTURE_HISTORY_LIMIT = 20
STRUCTURE_HISTORY_FIELDS = (
    'sections', 'blocks', 'element_merges', 'section_overrides',
    'suppressed_sections', 'automatic_sections', 'issues',
    'rejected_sections', 'reviewed_elements', 'usage',
    'section_analysis_status', 'pending_review', 'structure_profile',
)


class BookService:
    def __init__(self, book_id, repository=None):
        self.book_id = book_id
        self.repository = repository or get_repository()
        # build_scenes выполняет LLM-вызовы вне транзакций. При подготовке
        # книги несколько глав могут считаться параллельно, но их checkpoints
        # должны сливаться в общий snapshot строго по одному.
        self._scene_save_lock = Lock()

    def snapshot(self):
        return self.repository.load(self.book_id)

    def _structure(self):
        if hasattr(self.repository, 'load_structure'):
            return self.repository.load_structure(self.book_id)
        result, _ = self.snapshot()
        return result

    def outline(self):
        if hasattr(self.repository, 'outline') and hasattr(self.repository, 'load_structure'):
            result = self.repository.load_structure(self.book_id)
            return {'book_id': self.book_id, 'revision': result['revision'],
                    'sections': self.repository.outline(self.book_id)}
        result, _ = self.snapshot()
        return {'book_id': self.book_id, 'revision': result['revision'],
                'sections': [dict(s, scene_status=result.get('scenes', {}).get(
                    s['id'], {}).get('status', 'not_started')) for s in result['sections']]}

    def review_items(self, result=None):
        """Исходные issues с навигацией, без фильтрации и перефразирования."""
        if result is None:
            result = self._structure()
        sections = result.get('sections', [])
        items = []
        for raw_issue in result.get('issues', []):
            message = str(raw_issue)
            section_match = re.search(r"Начало раздела\s+(\d+)", message)
            position_match = re.search(
                r"(?:['\"]?position['\"]?\s*[:=]|\bpositions?\s+)\s*(\d+)",
                message,
                re.IGNORECASE,
            )
            section_index = None
            if section_match:
                section_start = int(section_match.group(1))
                same_start = [
                    (index, section) for index, section in enumerate(sections)
                    if section.get('start') == section_start
                ]
                if same_start:
                    section_index = max(
                        same_start, key=lambda pair: pair[1].get('level', 0)
                    )[0]
            target = int(position_match.group(1)) if position_match else None
            if section_index is None and target is not None:
                candidates = [
                    (index, section) for index, section in enumerate(sections)
                    if section.get('start', target + 1) <= target
                    and (section.get('end') is None or target < section['end'])
                ]
                if candidates:
                    section_index = max(candidates, key=lambda pair: pair[1].get('level', 0))[0]
            section = sections[section_index] if section_index is not None else None
            section_blocks = sorted(
                (block for block in result.get('blocks', [])
                 if section is not None and block.get('section_id') == section.get('id')),
                key=lambda block: block.get('start', 0),
            )
            if target is None and section is not None:
                opening_block = next(
                    (block for block in section_blocks if block.get('role') == 'epigraph'),
                    None,
                )
                target = (
                    opening_block.get('start') if opening_block else
                    section.get('body_start') if isinstance(section.get('body_start'), int) else
                    section.get('start')
                )

            target_block = next(
                (block for block in section_blocks
                 if block.get('start', -1) <= target < block.get('end', -1)),
                None,
            ) if target is not None else None
            block_type = 'unknown'
            target_start = target
            target_end = target + 1 if target is not None else None
            if section is not None and target == section.get('title_position'):
                block_type = 'heading'
            elif target_block is not None:
                target_start, target_end = target_block['start'], target_block['end']
                if target_block.get('role') == 'epigraph':
                    attribution = target_block.get('attribution_start')
                    block_type = (
                        'attribution' if isinstance(attribution, int) and target >= attribution
                        else 'epigraph'
                    )
                    if block_type == 'attribution':
                        target_start = attribution
                elif target_block.get('role') == 'footnotes':
                    block_type = 'attribution'
            elif section is not None and target is not None:
                body_start = section.get('body_start')
                block_type = 'body' if isinstance(body_start, int) and target >= body_start else 'opening'
            items.append({
                'message': message,
                'index': str(section_index) if section_index is not None else '',
                'section_title': (section.get('title') if section else None) or '',
                'position': str(target_start) if target_start is not None else '',
                'end_position': str(target_end) if target_end is not None else '',
                'block_type': block_type,
            })
        return items

    def search(self, query, limit=20):
        return self.repository.search(self.book_id, query, limit)

    def read_text(self, start_char, end_char, limit=12000):
        return self.repository.read_text(self.book_id, start_char, end_char, limit)

    def read_section(self, section_id, offset=0, limit=12000):
        if hasattr(self.repository, 'section_range'):
            section = self.repository.section_range(self.book_id, section_id)
            start, end = section['start_char'], section['end_char']
            revision = section['revision']
            # Совместимость на время между выкладкой кода и запуском миграции.
            if start is None:
                _, elements = self.snapshot()
                start = sum(len(e['text']) + 1 for e in elements[:section['start']])
                end = (start + len('\n'.join(
                    e['text'] for e in elements[section['start']:section['end']]
                )) if section['end'] is not None else None)
        else:
            result, elements = self.snapshot()
            raw = self._section(result, section_id)
            start = sum(len(e['text']) + 1 for e in elements[:raw['start']])
            end = (start + len('\n'.join(e['text'] for e in elements[raw['start']:raw['end']]))
                   if raw['end'] is not None else None)
            revision = result['revision']
        if end is None:
            raise ValueError('Конец раздела ещё не определён')
        if type(offset) is not int or not 0 <= offset < end - start:
            raise ValueError('offset вне раздела')
        return dict(self.read_text(start+offset, end, limit), section_id=section_id, revision=revision)

    def scenes(self, chapter_id, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Неверные параметры страницы сцен')
        result = self._structure()
        self._section(result, chapter_id)
        layer = result.get('scenes', {}).get(chapter_id, {'status': 'not_started', 'items': []})
        # Устаревшие сцены не выдаём агенту как свидетельства из текущей главы.
        items = layer['items'] if layer['status'] != 'stale' else []
        return dict(chapter_id=chapter_id, revision=result['revision'], status=layer['status'],
                    human_edited=layer.get('human_edited', False), issues=layer.get('issues', []),
                    items=items[offset:offset+limit], total=len(items),
                    next_offset=offset+limit if offset+limit < len(items) else None)

    def read_scene(self, chapter_id, scene_id, offset=0, limit=12000):
        result = self._structure()
        self._section(result, chapter_id)
        layer = result.get('scenes', {}).get(chapter_id)
        if not layer or layer['status'] not in {'ready', 'partial'}:
            raise ValueError('Нет актуальной разметки сцен')
        scene = next((s for s in layer['items'] if s['id'] == scene_id), None)
        if scene is None:
            raise ValueError('Сцена не найдена в этой главе')
        if type(offset) is not int or not 0 <= offset < scene['end_char'] - scene['start_char']:
            raise ValueError('offset вне сцены')
        return dict(self.read_text(scene['start_char']+offset, scene['end_char'], limit),
                    scene_id=scene_id, chapter_id=chapter_id, revision=result['revision'])

    @staticmethod
    def _section(result, section_id):
        section = next((s for s in result['sections'] if s['id'] == section_id), None)
        if section is None:
            raise ValueError('Раздел не найден в этой книге')
        return section

    def _current(self, expected_revision):
        result, elements = self.snapshot()
        if type(expected_revision) is not int or result['revision'] != expected_revision:
            raise ConflictError('Книга изменена; перечитай текущую версию')
        return result, elements

    @staticmethod
    def _remember_structure(result, previous, label):
        snapshot = {
            key: deepcopy(previous[key])
            for key in STRUCTURE_HISTORY_FIELDS if key in previous
        }
        history = deepcopy(previous.get('structure_history', []))
        history.append({'label': label, 'snapshot': snapshot})
        result['structure_history'] = history[-STRUCTURE_HISTORY_LIMIT:]

    def save(self, result, expected_revision, *, history_label=None, previous=None):
        if result['document_id'] != self.book_id:
            raise ValueError('Нельзя сохранить другую книгу через этот сервис')
        if history_label:
            if previous is None:
                previous, _ = self._current(expected_revision)
            self._remember_structure(result, previous, history_label)
        invalidate_scenes(result)
        result['revision'] = self.repository.save(result, expected_revision)
        return result

    def undo_structure(self, expected_revision):
        result, _ = self._current(expected_revision)
        history = deepcopy(result.get('structure_history', []))
        if not history:
            raise ValueError('Нет изменений, которые можно отменить')
        entry = history.pop()
        snapshot = entry.get('snapshot')
        if not isinstance(snapshot, dict):
            raise ValueError('История изменений повреждена')
        restored = deepcopy(result)
        for key in STRUCTURE_HISTORY_FIELDS:
            if key in snapshot:
                restored[key] = deepcopy(snapshot[key])
            else:
                restored.pop(key, None)
        restored['structure_history'] = history
        return self.save(restored, expected_revision)

    def update_section(self, section_id, changes, expected_revision):
        allowed = {'start', 'end', 'title', 'title_position', 'role', 'level'}
        if not changes or not set(changes) <= allowed:
            raise ValueError('Неизвестные поля раздела')
        result, elements = self._current(expected_revision)
        previous = deepcopy(result)
        sections = deepcopy(result['sections'])
        self._section({'sections': sections}, section_id).update(changes)
        return self.save(
            apply_human_sections(result, elements, sections), expected_revision,
            history_label='Изменение раздела', previous=previous,
        )

    def replace_sections(self, sections, expected_revision):
        """Полная атомарная правка: добавление, удаление и перемещение разделов."""
        result, elements = self._current(expected_revision)
        previous = deepcopy(result)
        return self.save(
            apply_human_sections(result, elements, sections), expected_revision,
            history_label='Изменение границ разделов', previous=previous,
        )

    def update_opening(self, section_id, operation, start, end, expected_revision):
        """Исправить эпиграф/body по нативному выделению полных элементов."""
        if operation not in {'epigraph', 'attribution', 'opening', 'body', 'footnotes', 'clear_epigraph'}:
            raise ValueError('Неизвестное действие с началом раздела')
        result, elements = self._current(expected_revision)
        previous = deepcopy(result)
        section = self._section(result, section_id)
        if operation != 'clear_epigraph':
            if (type(start) is not int or type(end) is not int or start >= end
                    or start < section['start'] or section['end'] is None
                    or end > section['end']):
                raise ValueError('Выделение выходит за границы выбранного раздела')
            if section.get('title_position') is not None and start <= section['title_position'] < end:
                raise ValueError('Заголовок нельзя включить в эпиграф или основной текст')

        blocks = deepcopy(result.get('blocks', []))
        current = [block for block in blocks
                   if block.get('section_id') == section_id and block.get('role') == 'epigraph']
        if operation == 'footnotes':
            removed_epigraphs = [block for block in current
                                 if block['start'] < end and block['end'] > start]
            blocks = [block for block in blocks
                      if block.get('section_id') != section_id
                      or block['end'] <= start or block['start'] >= end]
            if removed_epigraphs and section.get('body_start') == max(
                block['end'] for block in current
            ):
                first = section['start'] + (section.get('title_position') == section['start'])
                remaining = [block['end'] for block in blocks
                             if block.get('section_id') == section_id
                             and block.get('role') == 'epigraph']
                section['body_start'] = max([first, *remaining])
            key = f"{self.book_id}:footnotes:{section_id}:{start}:{end}"
            blocks.append({
                'id': f'{self.book_id}:b:{uuid5(NAMESPACE_URL, key).hex}',
                'section_id': section_id, 'role': 'footnotes',
                'start': start, 'end': end, 'source': 'human',
            })
        elif operation == 'epigraph':
            body_start = section.get('body_start')
            for position in range(section['start'], start):
                if (position == section.get('title_position')
                        or elements[position].get('type') == 'Title'
                        or isinstance(body_start, int) and position < body_start
                        or any(block['start'] <= position < block['end'] for block in current)):
                    continue
                raise ValueError(
                    'Эпиграф можно разместить только перед основным текстом главы. '
                    'Выберите фрагмент в её начале.'
                )
            # Сохраняем другие эпиграфы раздела. Заменяется только блок,
            # пересекающий новое выделение: смежные эпиграфы допустимы.
            overlapping = [
                block for block in current
                if block['start'] < end and start < block['end']
            ]
            blocks = [block for block in blocks
                      if block not in overlapping and (
                          block.get('section_id') != section_id
                          or block.get('role') != 'footnotes'
                          or block['end'] <= start or block['start'] >= end)]
            key = f"{self.book_id}:epigraph:{section_id}:{start}:{end}"
            blocks.append({
                'id': f'{self.book_id}:b:{uuid5(NAMESPACE_URL, key).hex}',
                'section_id': section_id, 'role': 'epigraph',
                'start': start, 'end': end, 'attribution_start': None,
                'source': 'human',
            })
            section['body_start'] = max(
                block['end'] for block in blocks
                if block.get('section_id') == section_id and block.get('role') == 'epigraph'
            )
        elif operation == 'attribution':
            block = next((block for block in current
                          if block['start'] < start and start <= block['end']), None)
            if block is None:
                raise ValueError('Сначала выделите цитату вместе с источником и отметьте её как эпиграф')
            if end > block['end']:
                block['end'] = end
            block['attribution_start'] = start
            block['source'] = 'human'
            section['body_start'] = max(section.get('body_start') or end, end)
        elif operation == 'body':
            removed_note = any(
                block.get('section_id') == section_id
                and block.get('role') == 'footnotes'
                and block['start'] < end and block['end'] > start
                for block in blocks
            )
            if not removed_note:
                section['body_start'] = start
            blocks = [block for block in blocks if (
                block.get('section_id') != section_id
                or block.get('role') not in {'epigraph', 'footnotes'}
                or block['end'] <= start or block['start'] >= end
            ) and (block not in current or block['end'] <= start)]
        elif operation == 'opening':
            blocks = [
                block for block in blocks
                if block not in current
                or block['end'] <= start
                or block['start'] >= end
            ]
            remaining = [
                block for block in blocks
                if block.get('section_id') == section_id and block.get('role') == 'epigraph'
            ]
            section['body_start'] = max([end, *(block['end'] for block in remaining)])
        else:
            blocks = [block for block in blocks if block not in current]
            first = section['start'] + (section.get('title_position') == section['start'])
            section['body_start'] = min(first, section['end'])

        result['blocks'] = blocks
        section_issue = re.compile(
            rf"Начало раздела\s+{section['start']}(?!\d)", re.IGNORECASE,
        )
        result['issues'] = [
            issue for issue in result.get('issues', [])
            if not section_issue.search(str(issue))
        ]
        result['pending_review'] = True
        result['section_analysis_status'] = (
            'needs_review' if result['issues'] else 'ready'
        )
        labels = {
            'epigraph': 'Разметка эпиграфа',
            'attribution': 'Разметка источника эпиграфа',
            'opening': 'Изменение вступления',
            'body': 'Изменение начала основного текста',
            'footnotes': 'Разметка примечания',
            'clear_epigraph': 'Удаление разметки эпиграфа',
        }
        return self.save(
            result, expected_revision,
            history_label=labels[operation], previous=previous,
        )

    def update_scene(self, chapter_id, scene_id, operation, expected_revision, **changes):
        result, elements = self._current(expected_revision)
        self._section(result, chapter_id)
        updated = edit_scenes(result, elements, chapter_id, scene_id, operation, **changes)
        return self.save(updated, expected_revision)

    def sections_without_scene_text(self, chapter_ids):
        """Разделы, в которых после исключения эпиграфов и примечаний не осталось
        основного текста (например, «Примечания»): сцены им не строятся."""
        result, elements = self.snapshot()
        sections = {section['id']: section for section in result['sections']}
        return [chapter_id for chapter_id in chapter_ids
                if chapter_id in sections
                and not has_scene_text(elements, sections[chapter_id],
                                       result.get('blocks', []))]

    def build_scenes(self, chapter_id, client, *, config=None, force=False,
                     replace_human=False, cache_dir='.cache/scenes', embedder=None):
        result, elements = self.snapshot()
        base = {key: deepcopy(value) for key, value in result.items()
                if key not in {'revision', 'scenes'}}

        def checkpoint(updated):
            # `updated` был получен до параллельных правок других глав. Нельзя
            # сохранять его целиком: отсутствующий в нём свежий SceneLayer был
            # бы удалён repository.save(). Перечитываем книгу и переносим лишь
            # слой текущей главы. Lock сериализует записи, а повтор нужен на
            # случай внешней правки между read и CAS-save.
            with self._scene_save_lock:
                last_error = None
                for _attempt in range(3):
                    current, _ = self.snapshot()
                    current_base = {key: value for key, value in current.items()
                                    if key not in {'revision', 'scenes'}}
                    if current_base != base:
                        raise ConflictError(
                            'Структура книги изменена во время построения сцен; повтори запрос'
                        )
                    merged = deepcopy(current)
                    merged.setdefault('scenes', {})[chapter_id] = deepcopy(
                        updated.get('scenes', {})[chapter_id]
                    )
                    try:
                        saved = self.save(merged, current['revision'])
                    except ConflictError as error:
                        last_error = error
                        continue
                    updated['revision'] = saved['revision']
                    return
                raise last_error

        return build_scenes(result, elements, chapter_id, client, config=config,
                            force=force, replace_human=replace_human,
                            cache_dir=cache_dir, checkpoint=checkpoint,
                            embedder=embedder)

    def merge_elements(self, start, end, expected_revision):
        result, elements = self._current(expected_revision)
        previous = deepcopy(result)
        if type(start) is not int or type(end) is not int or start >= end:
            raise ValueError("Нужны два разных элемента")
        if start < 0 or end > len(elements):
            raise ValueError("Диапазон выходит за пределы текста")
        union_start, union_end = start, end
        keep = []
        for m in result.get("element_merges", []):
            if m["end"] <= union_start or m["start"] >= union_end:
                keep.append(m)
            else:
                union_start = min(union_start, m["start"])
                union_end = max(union_end, m["end"])
        if union_end - union_start < 2:
            raise ValueError("Нечего объединять")
        keep.append({"start": union_start, "end": union_end})
        keep.sort(key=lambda m: m["start"])
        result["element_merges"] = keep
        result["pending_review"] = True
        return self.save(
            result, expected_revision,
            history_label='Объединение абзацев', previous=previous,
        )

    def unmerge_at(self, position, expected_revision):
        result, _ = self._current(expected_revision)
        previous = deepcopy(result)
        merges = result.get("element_merges", [])
        keep = [m for m in merges if not (m["start"] <= position < m["end"])]
        if len(keep) == len(merges):
            raise ValueError("Здесь нет объединения")
        result["element_merges"] = keep
        result["pending_review"] = True
        return self.save(
            result, expected_revision,
            history_label='Разъединение блока', previous=previous,
        )
