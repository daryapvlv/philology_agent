"""Чистые функции представления: понятные тексты вместо технических имён.

Без обращения к Neo4j/LLM — только форматирование того, что уже вернули
сервисы, поэтому проверяется юнит-тестами без Reflex.
"""
from application.book_preparation import PreparationError, describe_error

STAGE_LABELS = {
    'embeddings_check': 'Проверка провайдера эмбеддингов',
    'scenes': 'Сцены',
    'chunks': 'Фрагменты текста',
    'embeddings': 'Эмбеддинги',
    'mentions': 'Упоминания персонажей и мест',
    'chapter_entities': 'Персонажи и места глав',
    'book_entities': 'Персонажи и места книги',
    'reconciliation': 'Проверка повторов',
    'narrative': 'Нарративная разметка',
    'narrative_links': 'Связи между сценами и событиями',
}

# Какая строка чек-листа «идёт», пока prepare() сообщает этот ключ.
_PROGRESS_ROW = {'embeddings_check': 'embeddings', 'reconciliation': 'book_entities'}

ENTITY_TYPE_LABELS = {
    'person': 'персонаж', 'location': 'место', 'organization': 'организация',
    'group': 'группа', 'institution': 'учреждение', 'vehicle': 'предмет',
    'artifact': 'предмет', 'object': 'предмет', 'other': 'другое',
}

ANSWER_FOOTER = '\n---\n*Как искала.*'


def entity_type_label(value):
    return ENTITY_TYPE_LABELS.get(value or '', 'тип не определён')


def progress_row(key):
    return _PROGRESS_ROW.get(key, key)


def progress_text(key, position, total, chapter_title=None):
    """«Сцены: 2 из 5 · II. Вожатый» — строка текущего шага подготовки."""
    label = STAGE_LABELS.get(key, key)
    step = f'{position} из {total}' if total and total > 1 else ''
    parts = [part for part in (step, chapter_title) if part]
    return f'{label}: ' + ' · '.join(parts) if parts else f'{label}…'


def checklist(status, *, with_embeddings, narrative_enabled):
    """Строки чек-листа подготовки по уже сохранённым счётчикам.

    status — словарь Book.preparation_status. state: done | todo; «идёт» и
    «ошибка» накладывает интерфейс по живому прогрессу."""
    chapters = status.get('chapters', 0)
    scene_chapters = status.get('scene_chapters', 0)
    scenes = status.get('scenes', 0)
    chunks = status.get('chunks', 0)
    all_scenes = bool(chapters) and scene_chapters == chapters
    rows = [
        ('scenes', f'{scene_chapters}/{chapters} глав', all_scenes),
        ('chunks', f'{chunks}', all_scenes and chunks > 0),
    ]
    if with_embeddings:
        embedded = status.get('embedded_chunks', 0)
        rows.append(('embeddings', f'{embedded}/{chunks}', chunks > 0 and embedded == chunks))
    extracted = status.get('extracted_scenes', 0)
    resolved = status.get('chapter_entity_chapters', 0)
    entities = status.get('book_entities', 0)
    rows += [
        ('mentions', f'{extracted}/{scenes} сцен', scenes > 0 and extracted == scenes),
        ('chapter_entities', f'{resolved}/{chapters} глав', bool(chapters) and resolved == chapters),
        ('book_entities', f'{entities} · связей со сценами {status.get("presence_edges", 0)}',
         entities > 0),
    ]
    if narrative_enabled:
        total = status.get('narrative_scenes', 0)
        annotated = status.get('annotated_scenes', 0)
        narrative_done = total > 0 and annotated == total
        rows += [
            ('narrative', f'{annotated}/{total} сцен', narrative_done),
            ('narrative_links', f'сцен {status.get("scene_links", 0)} · '
                                f'событий {status.get("narrative_links", 0)}', narrative_done),
        ]
    return [dict(key=key, label=STAGE_LABELS[key], count=count, state='done' if done else 'todo')
            for key, count, done in rows]


def chapter_table(status, chapter_titles=None, *, with_embeddings=True, failures=()):
    """Плоские строки для таблицы «главы × стадии».

    failures — ошибки последнего запуска; сохранённые счётчики остаются основой,
    а ошибка накладывается на конкретную ячейку и не теряется после reload UI.
    """
    titles = chapter_titles or {}
    failed = {item.get('chapter_id'): item for item in failures}
    rows = []
    for raw in status.get('chapter_rows', []):
        chapter_id = raw['chapter_id']
        scenes = raw.get('scenes', 0)
        chunks = raw.get('chunks', 0)
        extracted = raw.get('extracted_scenes', 0)
        scene_state = 'done' if raw.get('scene_status') == 'ready' else 'todo'
        chunk_done = (scene_state == 'done' and chunks > 0
                      and (not with_embeddings
                           or raw.get('embedded_chunks', 0) == chunks))
        mention_done = scenes > 0 and extracted == scenes
        entity_done = raw.get('chapter_entity_status') == 'ready'
        error = failed.get(chapter_id)
        states = {
            'scenes': scene_state,
            'chunks': 'done' if chunk_done else 'todo',
            'mentions': 'done' if mention_done else 'todo',
            'chapter_entities': 'done' if entity_done else 'todo',
        }
        if error and error.get('key') in states:
            states[error['key']] = 'error'
        embedding_count = (f"{raw.get('embedded_chunks', 0)}/{chunks} эмб."
                           if with_embeddings else f'{chunks}')
        rows.append({
            'chapter_id': chapter_id,
            'title': titles.get(chapter_id, chapter_id),
            'scenes_state': states['scenes'],
            'scenes_text': str(scenes),
            'chunks_state': states['chunks'],
            'chunks_text': embedding_count,
            'mentions_state': states['mentions'],
            'mentions_text': f'{extracted}/{scenes}',
            'chapter_entities_state': states['chapter_entities'],
            'chapter_entities_text': 'готово' if entity_done else '—',
            'error': error.get('message', '') if error else '',
        })
    return rows


def technical_details(status):
    """Строка «Подробности»: прежние технические имена слоёв, для диагностики."""
    line = (
        f"NarrativeScene: {status.get('scene_chapters', 0)}/{status.get('chapters', 0)} глав · "
        f"TextChunk: {status.get('chunks', 0)} "
        f"(embeddings {status.get('embedded_chunks', 0)}/{status.get('chunks', 0)}) · "
        f"EntityMention: {status.get('extracted_scenes', 0)}/{status.get('scenes', 0)} сцен · "
        f"ChapterEntity: {status.get('chapter_entity_chapters', 0)}/{status.get('chapters', 0)} глав · "
        f"BookEntity: {status.get('book_entities', 0)} · PRESENT_IN: {status.get('presence_edges', 0)}"
    )
    if 'narrative_scenes' in status:
        line += (f" · NarrativeEvent: {status['annotated_scenes']}/{status['narrative_scenes']} сцен"
                 f" · SCENE_LINK: {status.get('scene_links', 0)}"
                 f" · NARRATIVE_LINK: {status['narrative_links']}")
    return line


def readable_error(error, chapter_titles=None):
    """Понятный текст сбоя подготовки: стадия, глава по названию, причина."""
    if not isinstance(error, PreparationError) or not error.key:
        return describe_error(error)
    label = STAGE_LABELS.get(error.key, error.stage)
    where = []
    if error.chapter_id:
        title = (chapter_titles or {}).get(error.chapter_id)
        where.append(f'глава «{title}»' if title else 'выбранная глава')
    if error.scene:
        position, _, total = error.scene.partition('/')
        where.append(f'сцена {position} из {total}')
    prefix = label + (f' ({", ".join(where)})' if where else '')
    return f'{prefix}: {error.detail}'


def split_answer(content):
    """Ответ исследования и его подвал «Как искала» (synthesize.footer).

    Только представление: сохранённое сообщение не меняется, поэтому старые
    чаты тоже раскладываются. Без подвала — весь текст считается ответом."""
    text = content or ''
    body, marker, footer = text.partition(ANSWER_FOOTER)
    if not marker:
        return text, ''
    return body.rstrip(), footer.strip()
