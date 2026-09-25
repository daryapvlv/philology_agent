"""Проверка целостности полного структурного снимка книги."""

from .scenes import chapter_input, chapter_units, validate_scenes
from .sections import BLOCK_ROLES, PARENT_ROLES, validate_section
from .blocks import validate_blocks

def validate_merges(result, elements):
    merges = result.get("element_merges", [])
    if not isinstance(merges, list):
        raise ValueError("element_merges должен быть списком")
    prev_end = 0
    for m in sorted(merges, key=lambda x: x["start"]):
        start, end = m.get("start"), m.get("end")
        if type(start) is not int or type(end) is not int or start >= end:
            raise ValueError("Некорректный диапазон объединения")
        if end - start < 2:
            raise ValueError("Объединение должно охватывать минимум 2 элемента")
        if start < prev_end:
            raise ValueError("Объединения пересекаются")
        if end > len(elements):
            raise ValueError("Объединение выходит за пределы текста")
        prev_end = end

def validate_snapshot(result, elements):
    validate_merges(result, elements)
    sections = {section["id"]: section for section in result["sections"]}
    if len(sections) != len(result["sections"]):
        raise ValueError("Повторные ID разделов")
    visible = [dict(position=index, text=element["text"])
               for index, element in enumerate(elements)]
    for section in sections.values():
        validate_section(section, visible)
        parent_id = section.get("parent_id")
        if parent_id is not None:
            parent = sections.get(parent_id)
            if parent is None or (
                section["role"] not in BLOCK_ROLES
                and parent["level"] >= section["level"]
            ) or parent["start"] > section["start"]:
                raise ValueError("Некорректный родитель раздела")
            allowed = PARENT_ROLES.get(section["role"])
            if (section["role"] not in BLOCK_ROLES and allowed is not None
                    and parent["role"] not in allowed):
                raise ValueError("Недопустимая литературная иерархия")
            if (parent["end"] is not None and section["end"] is not None
                    and section["end"] > parent["end"]):
                raise ValueError("Раздел выходит за границы родителя")

    validate_blocks(result, elements)

    scene_ids = set()
    for chapter_id, layer in result.get("scenes", {}).items():
        if layer["status"] not in {"ready", "partial", "failed", "stale"}:
            raise ValueError("Неизвестное состояние сцен")
        for scene in layer["items"]:
            if scene["id"] in scene_ids or scene["chapter_id"] != chapter_id:
                raise ValueError("Повторные ID сцен или неверная глава")
            scene_ids.add(scene["id"])
        if layer["status"] in {"ready", "partial"} and layer["items"]:
            chapter = sections.get(chapter_id)
            if chapter is None or layer["input_hash"] != chapter_input(result, chapter):
                raise ValueError("Сцены относятся к другой версии главы")
            units = chapter_units(elements, chapter, result.get("blocks", []))
            end = (units[-1]["end_char"] if layer["status"] == "ready"
                   else layer["items"][-1]["end_char"])
            if end > units[-1]["end_char"]:
                raise ValueError("Сцены выходят за границы главы")
            validate_scenes(layer["items"], units[0]["start_char"], end, chapter_id, units)
        elif layer["status"] == "ready":
            raise ValueError("Готовый слой должен содержать сцены")
