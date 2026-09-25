"""Ручные операции над структурой; входные элементы передаются явно."""
from copy import deepcopy
import re

from ...rules.sections import apply_human_sections


def _apply(result, sections, selected_id, elements):
    """Общий хвост для edit_section и split_section."""
    updated = apply_human_sections(result, elements, sections)
    index = next(
        i for i, s in enumerate(updated["sections"]) if s["id"] == selected_id
    )
    return updated, str(index)


def edit_section(result, index, values, *, elements):
    sections = deepcopy(result["sections"])
    section = sections[int(index)]
    section["title"] = values["title"].strip() or None
    section["role"] = values["role"]
    for field in ("start", "end", "title_position", "level"):
        value = values[field].strip()
        section[field] = int(value) if value else None
    if section["start"] is None or section["level"] is None:
        raise ValueError("Начало и уровень обязательны")
    if section["end"] is not None:
        first_content = section["start"] + (
            section.get("title_position") == section["start"]
        )
        body_start = section.get("body_start")
        if (type(body_start) is not int
                or body_start < section["start"]
                or body_start > section["end"]):
            section["body_start"] = min(first_content, section["end"])
        body_end = section.get("body_end")
        if (type(body_end) is not int
                or body_end < section["body_start"]
                or body_end > section["end"]):
            section["body_end"] = section["end"]
    return _apply(result, sections, section["id"], elements)


def add_section(result, values, *, elements):
    sections = deepcopy(result["sections"])
    section = {
        "title": values["title"].strip() or None,
        "role": values["role"],
        "source": "human",
    }
    for field in ("start", "end", "title_position", "level"):
        value = values[field].strip()
        section[field] = int(value) if value else None
    if section["start"] is None or section["end"] is None or section["level"] is None:
        raise ValueError("Для нового раздела обязательны начало, конец и уровень")
    first_content = section["start"] + (
        section.get("title_position") == section["start"]
    )
    section["body_start"] = min(first_content, section["end"])
    section["body_end"] = section["end"]
    sections.append(section)
    updated = apply_human_sections(result, elements, sections)
    selected = next(
        index for index, item in enumerate(updated["sections"])
        if item["start"] == section["start"]
        and item["end"] == section["end"]
        and item["role"] == section["role"]
        and item["level"] == section["level"]
    )
    return updated, str(selected)


def split_section(result, elements, index, boundaries):
    section = result["sections"][int(index)]
    if section["end"] is None:
        raise ValueError("Сначала укажи конец раздела.")
    try:
        points = [int(s) for s in re.split(r"[,;\s]+", boundaries.strip()) if s]
    except ValueError:
        raise ValueError("Введи номера элементов через запятую или с новой строки.") from None
    if not points or len(points) != len(set(points)):
        raise ValueError("Укажи хотя бы одну границу, без повторений.")
    points.sort()
    if any(not section["start"] < p < section["end"] for p in points):
        raise ValueError("Все новые границы должны находиться строго внутри выбранного раздела.")
    make_chapters = section["role"] == "work"
    others = [deepcopy(s) for i, s in enumerate(result["sections"])
              if i != int(index) or make_chapters]
    if any(s["start"] in points for s in others):
        raise ValueError("На одной из границ уже начинается раздел. Выбери другой номер.")
    for child in others:
        if section["start"] < child["start"] < section["end"]:
            if child["end"] is None or any(child["start"] < p < child["end"] for p in points):
                raise ValueError("Граница пересекает вложенный раздел. Сначала раздели его или уточни его конец.")
    starts = [section["start"]] + points
    for i, (start, end) in enumerate(zip(starts, points + [section["end"]])):
        part = deepcopy(section)
        part.update(start=start, end=end)
        if make_chapters:
            part.pop("id", None)
            part.update(role="chapter", level=section["level"] + 1,
                        title=None, title_position=None)
        if i:
            part.pop("id", None)
            heading = elements[start]["text"].strip()
            part["title"] = heading if 0 < len(heading) <= 180 else None
            part["title_position"] = start if part["title"] else None
        elif part["title_position"] is not None and part["title_position"] >= end:
            part.update(title=None, title_position=None)
        others.append(part)
    return _apply(result, others, section["id"], elements)
