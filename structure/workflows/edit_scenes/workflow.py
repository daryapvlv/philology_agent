"""Атомарные правки сцен. Координаты — символы канонического текста книги."""
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

from ...rules.scenes import chapter_input, chapter_units, validate_scenes


def edit_scenes(result, elements, chapter_id, scene_id, operation, *,
                title=None, summary=None, boundary=None):
    """edit, split, merge_next или move_boundary (граница с правым соседом).

    После изменения границ описания сохраняются как черновики; их можно исправить
    операцией edit. Весь ручной слой защищён от автоматической перезаписи.
    """
    updated = deepcopy(result)
    chapter = next(s for s in updated['sections'] if s['id'] == chapter_id)
    layer = updated.get('scenes', {}).get(chapter_id)
    if not layer or layer['status'] != 'ready' or layer['input_hash'] != chapter_input(updated, chapter):
        raise ValueError('Сначала построй актуальные сцены всей главы')
    items = layer['items']
    index = next((i for i, s in enumerate(items) if s['id'] == scene_id), None)
    if index is None:
        raise ValueError('Сцена не найдена в выбранной главе')
    scene = items[index]
    if operation == 'edit':
        if title is not None:
            scene['title'] = title.strip()
        if summary is not None:
            scene['summary'] = summary.strip()
        scene['description_stale'] = False
    elif operation == 'split':
        if type(boundary) is not int or not scene['start_char'] < boundary < scene['end_char']:
            raise ValueError('Граница должна находиться внутри сцены')
        right = dict(scene, id=f'{chapter_id}:scene:{uuid4().hex}', start_char=boundary)
        scene['end_char'] = boundary
        scene['description_stale'] = right['description_stale'] = True
        right['source'] = 'human'
        items.insert(index + 1, right)
    elif operation in {'merge_next', 'move_boundary'}:
        if index + 1 == len(items):
            raise ValueError('Справа нет соседней сцены')
        right = items[index + 1]
        if operation == 'merge_next':
            scene['end_char'] = right['end_char']
            scene['summary'] += '\n' + right['summary']
            items.pop(index + 1)
        else:
            if type(boundary) is not int or not scene['start_char'] < boundary < right['end_char']:
                raise ValueError('Граница должна находиться внутри двух соседних сцен')
            scene['end_char'] = right['start_char'] = boundary
            right.update(source='human', description_stale=True)
        scene['description_stale'] = True
    else:
        raise ValueError('Неизвестная операция над сценой')
    scene['source'] = 'human'
    units = chapter_units(elements, chapter, updated.get('blocks', []))
    validate_scenes(items, units[0]['start_char'], units[-1]['end_char'], chapter_id, units)
    canonical = '\n'.join(element['text'] for element in elements)
    for item in items:
        old_text_hash = item.get('text_hash')
        old_summary_hash = item.get('summary_hash')
        item['text'] = canonical[item['start_char']:item['end_char']]
        item['text_hash'] = sha256(item['text'].encode()).hexdigest()
        item['summary_hash'] = sha256((
            item['title'].strip() + '\n' + item['summary'].strip()
        ).encode()).hexdigest()
        if item['text_hash'] != old_text_hash:
            item.pop('embedding', None)
            item.pop('embedding_model', None)
        if item['summary_hash'] != old_summary_hash:
            item.pop('summary_embedding', None)
            item.pop('summary_embedding_model', None)
            item.pop('summary_embedding_hash', None)
    layer['human_edited'] = True
    return updated
