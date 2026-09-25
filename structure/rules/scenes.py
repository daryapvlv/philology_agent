"""Чистые правила адресации, проверки и актуальности сцен."""
import re

from infrastructure.files import json_sha256


def chapter_input(result, chapter):
    """Название не влияет на границы сцен; диапазон и принадлежность — влияют."""
    return json_sha256({
        "text": result.get("source", {}).get("sha256"),
        "chapter": {key: chapter.get(key) for key in
                    ("id", "start", "end", "title_position", "body_start",
                     "body_end", "role", "parent_id")},
        "excluded_blocks": sorted(
            (block["role"], block["start"], block["end"])
            for block in result.get("blocks", [])
            if block.get("section_id") == chapter["id"]
            and block.get("role") in {"epigraph", "footnotes"}
        ),
    })


def invalidate_scenes(result):
    """Сохраняем старые сцены для просмотра, но запрещаем считать их актуальными."""
    chapters = {s["id"]: s for s in result["sections"]}
    for section_id, layer in result.get("scenes", {}).items():
        chapter = chapters.get(section_id)
        if chapter is None or layer["input_hash"] != chapter_input(result, chapter):
            layer["status"] = "stale"


def has_scene_text(elements, chapter, blocks=()):
    """Есть ли у раздела основной текст для сцен: без эпиграфов и примечаний.

    Раздел «Примечания»/«Notes» целиком размечен как footnotes — его не
    индексируют, а пропускают при подготовке книги."""
    return any(elements[position]["text"].strip()
               for position in _body_positions(elements, chapter, blocks))


def _body_positions(elements, chapter, blocks):
    """Позиции элементов тела главы без эпиграфов и примечаний; битые границы —
    ошибка, а не «пустая глава»."""
    start = chapter.get("body_start")
    if start is None:
        start = chapter["start"] + (chapter.get("title_position") == chapter["start"])
    end = chapter.get("body_end")
    if end is None:
        end = chapter["end"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(elements):
        raise ValueError("Для сцен нужны известные корректные границы главы")
    excluded = {
        position
        for block in blocks
        if block.get("section_id") == chapter["id"]
        and block.get("role") in {"epigraph", "footnotes"}
        for position in range(max(start, block["start"]), min(end, block["end"]))
    }
    return [position for position in range(start, end) if position not in excluded]


def chapter_units(elements, chapter, blocks=()):
    """Предложения и их точные смещения в каноническом тексте книги."""
    positions = _body_positions(elements, chapter, blocks)
    if not positions or not any(elements[position]["text"].strip() for position in positions):
        raise ValueError("В главе нет текста")
    offsets = []
    offset = 0
    for element in elements:
        offsets.append(offset)
        offset += len(element["text"]) + 1
    units = []
    for index, position in enumerate(positions):
        text = elements[position]["text"]
        if index + 1 < len(positions):
            text += "\n"
        for match in re.finditer(r'.+?(?:[.!?…][»”"]?\s+|\n+|$)', text, re.DOTALL):
            units.append({"start_char": offsets[position] + match.start(),
                          "end_char": offsets[position] + match.end(),
                          "text": match.group()})
    return units


def validate_scenes(items, start, end, chapter_id, units=None):
    """Проверить полное непрерывное покрытие главы сценами."""
    if not items:
        raise ValueError("Нужна хотя бы одна сцена")
    cursor, ids = start, set()
    for scene in items:
        if not isinstance(scene.get("id"), str) or not scene["id"] or scene["id"] in ids:
            raise ValueError("ID сцен должны быть непустыми и уникальными")
        ids.add(scene["id"])
        if scene.get("chapter_id") != chapter_id:
            raise ValueError("Сцена принадлежит другой главе")
        left, right = scene.get("start_char"), scene.get("end_char")
        if type(left) is not int or type(right) is not int or left < cursor or not left < right <= end:
            raise ValueError("Сцены должны следовать без пропусков и пересечений")
        if left != cursor and (units is None or any(
            unit["start_char"] < left and unit["end_char"] > cursor for unit in units
        )):
            raise ValueError("Сцены пропускают текст основной части")
        if any(not isinstance(scene.get(key), str) or not scene[key].strip()
               for key in ("title", "summary")):
            raise ValueError("Нужны название и описание сцены")
        cursor = right
    if cursor != end:
        raise ValueError("Сцены должны покрывать главу целиком")
