"""Детерминированные TextChunk внутри границ NarrativeScene.

Модуль не зависит от Neo4j и retrieval: одну и ту же нарезку используют запись
сцен и последующая поисковая индексация/миграция старых книг.
"""
from hashlib import sha256
import re


_SENTENCE_END = re.compile(r'[.!?…]+[»")\]]*\s+')


def _paragraphs(text, start, stop):
    for match in re.finditer(r'[^\n]+', text[start:stop]):
        left = start + match.start()
        right = start + match.end()
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left < right:
            yield left, right


def _pieces(text, start, end, max_chars):
    """Абзац длиннее max_chars делится по границам предложений."""
    if end - start <= max_chars:
        yield start, end
        return
    cuts = [start + match.end() for match in _SENTENCE_END.finditer(text[start:end])]
    cursor = start
    while cursor < end:
        limit = cursor + max_chars
        if limit >= end:
            yield cursor, end
            return
        fitting = [cut for cut in cuts if cursor < cut <= limit]
        cut = fitting[-1] if fitting else None
        if cut is None:
            space = text.rfind(' ', cursor + 1, limit)
            cut = space + 1 if space > cursor else limit
        piece_end = cut
        while piece_end > cursor and text[piece_end - 1].isspace():
            piece_end -= 1
        yield cursor, piece_end
        cursor = cut
        while cursor < end and text[cursor].isspace():
            cursor += 1


def passages(text, min_chars=400, max_chars=1200, *, start=0, end=None,
             scene_id=None, chapter_id=None):
    """Неперекрывающиеся TextChunk строго внутри переданного диапазона сцены."""
    if (type(min_chars) is not int or type(max_chars) is not int
            or not 0 < min_chars <= max_chars or max_chars < 200):
        raise ValueError('Нужно 0 < min_chars <= max_chars и max_chars >= 200')
    stop = len(text) if end is None else end
    if type(start) is not int or type(stop) is not int or not 0 <= start <= stop <= len(text):
        raise ValueError('Неверный диапазон passages')
    current = None
    units = (piece for left, right in _paragraphs(text, start, stop)
             for piece in _pieces(text, left, right, max_chars))
    for left, right in units:
        if current is None:
            current = [left, right]
        elif current[1] - current[0] < min_chars and right - current[0] <= max_chars:
            current[1] = right
        else:
            yield _row(text, *current, scene_id=scene_id, chapter_id=chapter_id)
            current = [left, right]
    if current is not None:
        yield _row(text, *current, scene_id=scene_id, chapter_id=chapter_id)


def scene_chunks(text, scene, min_chars=400, max_chars=1200, ranges=None):
    """Нарезать сцену; каждый chunk остаётся внутри сцены и разрешённого body."""
    allowed = ranges or [dict(start=scene['start_char'], end=scene['end_char'])]
    rows = []
    for span in allowed:
        start = max(scene['start_char'], span['start'])
        end = min(scene['end_char'], span['end'])
        if start >= end or not text[start:end].strip():
            continue
        rows.extend(passages(
            text, min_chars, max_chars, start=start, end=end,
            scene_id=scene['id'], chapter_id=scene['chapter_id'],
        ))
    return rows


def _row(text, start, end, *, scene_id=None, chapter_id=None):
    fragment = text[start:end]
    digest = sha256(fragment.encode()).hexdigest()
    owner = scene_id or 'unscoped'
    row = dict(id=f'{owner}:{start}:{end}:{digest[:16]}', start_char=start, end_char=end,
               text=fragment, content_hash=digest)
    if scene_id is not None:
        row['scene_id'] = scene_id
    if chapter_id is not None:
        row['chapter_id'] = chapter_id
    return row
