"""Чистые правила проверки и сборки дерева разделов."""
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from .scenes import invalidate_scenes


HEADING_ROLES = frozenset({
    "collection", "work", "volume", "part", "chapter", "paragraph", "section",
    "act", "dramatic_scene", "picture", "dramatis_personae",
    "prologue", "epilogue", "preface", "appendix", "other",
})

# Единственные поддерживаемые цепочки дерева. Пустой набор означает корень.
PARENT_ROLES = {
    "collection": frozenset(),
    "work": frozenset({"collection"}),
    "volume": frozenset({"work"}),
    "part": frozenset({"work", "volume"}),
    "chapter": frozenset({"work", "volume", "part"}),
    "section": frozenset({"work", "volume", "part", "chapter"}),
    "paragraph": frozenset({"work", "volume", "part", "chapter", "section"}),
    "preface": frozenset({"collection", "work", "volume", "part"}),
    "prologue": frozenset({"work", "volume", "part"}),
    "epilogue": frozenset({"work", "volume", "part"}),
    "appendix": frozenset({"collection", "work", "volume", "part"}),
    "dramatis_personae": frozenset({"work"}),
    "act": frozenset({"work"}),
    "picture": frozenset({"act"}),
    "dramatic_scene": frozenset({"act", "picture"}),
    # Старые ручные разделы остаются допустимыми при миграции.
    "other": HEADING_ROLES,
}

BLOCK_ROLES = frozenset({
    "title_page", "dedication", "epigraph", "footnotes",
    "scene_break", "inserted_text", "contents",
})

ALL_ROLES = HEADING_ROLES | BLOCK_ROLES
ROLES = sorted(ALL_ROLES)


def section_key(section):
    return (section["start"], section["level"], section["role"])


def new_section_id(document_id, section):
    key = str((document_id, section_key(section)))
    return f"{document_id}:s:{uuid5(NAMESPACE_URL, key).hex}"


def validate_section(section, visible):
    """Проверяет одну запись. Ошибка этой записи не отменяет соседние."""
    by_position = {element["position"]: element for element in visible}
    start, end = section["start"], section["end"]
    if type(start) is not int or start not in by_position:
        raise ValueError("Начало раздела отсутствует во фрагменте")
    if end is not None and (
        type(end) is not int or end <= start
        or end > visible[-1]["position"] + 1
    ):
        raise ValueError("Неверная граница раздела")
    if type(section["level"]) is not int or section["level"] < 1:
        raise ValueError("level должен быть целым числом от 1")
    if section["role"] not in ALL_ROLES:
        raise ValueError("Неизвестная роль раздела")
    body_start, body_end = section.get("body_start"), section.get("body_end")
    if body_start is not None and (
        type(body_start) is not int or body_start < start
        or end is None or body_start > end
    ):
        raise ValueError("Неверное начало основного текста раздела")
    if body_end is not None and (
        type(body_end) is not int or body_start is None
        or body_end < body_start or end is None or body_end > end
    ):
        raise ValueError("Неверный конец основного текста раздела")
    epigraph_start, epigraph_end = section.get("epigraph_start"), section.get("epigraph_end")
    if (epigraph_start is None) != (epigraph_end is None):
        raise ValueError("Нужны обе границы эпиграфа")
    if epigraph_start is not None and (
        type(epigraph_start) is not int or type(epigraph_end) is not int
        or not start <= epigraph_start < epigraph_end == body_start
    ):
        raise ValueError("Некорректный эпиграф раздела")
    title, position = section["title"], section["title_position"]
    if title is None and position is None:
        return
    if (
        not isinstance(title, str) or not title.strip()
        or type(position) is not int
        or position not in by_position
        or position < start
        or (end is not None and position >= end)
        or " ".join(title.split())
           not in " ".join(by_position[position]["text"].split())
    ):
        raise ValueError("Заголовок не подтверждён исходным элементом")


def collect_sections(reply, visible, issues, rejected, origin="system", batch=None):
    """Проходит по reply['sections'], проверяет каждое предложение, добавляет
    валидные в accepted, невалидные — в rejected. Мутирует issues и rejected."""
    accepted = []
    if not isinstance(reply, dict) or not isinstance(reply.get("sections"), list):
        raise ValueError("Ответ должен содержать список sections")

    model_issues = reply.get("issues", [])
    if isinstance(model_issues, list):
        for item in model_issues:
            if isinstance(item, dict):
                message = item.get("comment", item.get("note", item.get("message", str(item))))
                prefix = f"[{item['element']}]: " if "element" in item else ""
                issues.append(prefix + str(message))
            else:
                issues.append(str(item))
    else:
        issues.append(f"Неверный формат issues: {model_issues!r}")

    for proposal in reply["sections"]:
        try:
            validate_section(proposal, visible)
            if batch is not None and not batch.start <= proposal["start"] < batch.end:
                raise ValueError("Начало находится в контексте, а не в основной части порции")
        except (ValueError, KeyError, TypeError) as error:
            rejected.append({"proposal": proposal, "source": origin, "reason": str(error)})
            issues.append(f"Пропущено предложение раздела: {error}")
            continue
        accepted.append(dict(proposal, source=origin))
    return accepted


def assemble_structure(proposals, elements, document_id, issues, rejected):
    """Собирает дерево заголовков. Сноски и эпиграфы не закрывают главу."""
    sections, starts = [], set()
    for proposal in sorted(proposals, key=lambda item: (item["start"], item["level"])):
        key = (proposal["start"], proposal["level"])
        if key in starts:
            rejected.append({
                "proposal": proposal,
                "source": proposal["source"],
                "reason": "Повторное начало раздела на том же уровне",
            })
            issues.append(f"Несколько разделов начинаются с элемента {proposal['start']}")
        else:
            starts.add(key)
            sections.append(dict(proposal))

    special_roles = BLOCK_ROLES
    headings = [s for s in sections if s["role"] not in special_roles]
    for i, heading in enumerate(headings):
        next_start = next(
            (item["start"] for item in headings[i + 1:]
             if item["level"] <= heading["level"]),
            len(elements),
        )
        if heading["end"] is None:
            heading["end"] = next_start
        elif heading["end"] > next_start:
            issues.append(
                f"Раздел {heading['start']}: граница пересекает следующий раздел"
            )

    stack = []
    for section in sections:
        start, end = section["start"], section["end"]
        is_special = section["role"] in special_roles
        while stack and (
            stack[-1]["end"] <= start
            or (not is_special and stack[-1]["level"] >= section["level"])
        ):
            stack.pop()
        parent = stack[-1] if stack else None
        if parent is not None and end is not None:
            if end > parent["end"] and not is_special:
                original = next(item for item in proposals if section_key(item) == section_key(section))
                if original["end"] is None:
                    section["end"] = end = parent["end"]
            if end > parent["end"]:
                issues.append(
                    f"Раздел {start}: выходит за границу предполагаемого родителя"
                )
                parent = None
        section.update(
            id=section.get("id") or new_section_id(document_id, section),
            parent_id=parent["id"] if parent else None,
        )
        if end is None:
            issues.append(f"Блок {start} ({section['role']}): конец пока неизвестен")
        if not is_special:
            stack.append(section)
    if len({s["id"] for s in sections}) != len(sections):
        raise ValueError("Идентификаторы разделов должны быть уникальны")
    return sections


def apply_human_sections(result, elements, sections) -> dict:
    """Применяет полный список sections к result. Не трогает файлы.

    Новые и изменённые разделы получают source=human; неизменённые
    сохраняют прежний источник.
    """
    result = deepcopy(result)
    visible = [{"position": i, "text": e["text"]} for i, e in enumerate(elements)]
    old_sections = result["sections"]
    automatic_sections = deepcopy(result.get("automatic_sections", old_sections))
    fields = ("start", "end", "title", "title_position", "level", "role")
    accepted, problems, rejected = [], [], []
    for proposal in sections:
        proposal = deepcopy(proposal)
        unchanged = next(
            (old for old in old_sections
             if isinstance(proposal, dict)
             and all(old.get(k) == proposal.get(k) for k in fields)),
            None,
        )
        if isinstance(proposal, dict) and not proposal.get("id") and unchanged:
            proposal["id"] = unchanged["id"]
        if isinstance(proposal, dict) and proposal.get("id") is not None:
            if not isinstance(proposal["id"], str) or not proposal["id"]:
                raise ValueError("id должен быть непустой строкой")
        origin = unchanged.get("source", "system") if unchanged else "human"
        accepted.extend(
            collect_sections(
                {"sections": [proposal]},
                visible,
                problems,
                rejected,
                origin,
            )
        )
    result["sections"] = assemble_structure(
        accepted, visible, result["document_id"],
        problems, rejected,
    )
    old_by_id = {section["id"]: section for section in old_sections}
    changed_ranges = set()
    for section in result["sections"]:
        old = old_by_id.get(section["id"])
        if old is None or (old["start"], old["end"]) != (
            section["start"], section["end"]
        ):
            changed_ranges.add(section["id"])
            for key in ("epigraph_start", "epigraph_end", "epigraph_attribution_start"):
                section.pop(key, None)
            if section["end"] is not None:
                first = section["start"] + (section.get("title_position") == section["start"])
                section["body_start"] = min(first, section["end"])
                section["body_end"] = section["end"]
    current_ids = {section["id"] for section in result["sections"]}
    result["blocks"] = [block for block in result.get("blocks", [])
                        if block.get("section_id") in {None, *current_ids}
                        and block.get("section_id") not in changed_ranges]
    # Неизвестный конец локального блока допустим и не блокирует правку главы.
    unknown_ends = {
        f"Блок {s['start']} ({s['role']}): конец пока неизвестен"
        for s in result["sections"] if s["end"] is None
    }
    errors = [message for message in problems if message not in unknown_ends]
    if errors or rejected:
        raise ValueError("Правка не принята: " + "; ".join(errors))
    result["issues"].extend(message for message in problems if message not in result["issues"])
    # Ручные решения хранятся отдельным слоем относительно последней полностью
    # автоматической версии. Поэтому новый запуск анализа можно применить заново,
    # не смешивая старые model/system sections со свежими.
    automatic_by_id = {section.get("id"): section for section in automatic_sections}
    current = {section["id"]: section for section in result["sections"]}
    suppressed = {
        section_key(section) for section in automatic_sections
        if section.get("id") not in current
    }
    override_fields = ("id", "start", "end", "title", "title_position", "level", "role")
    overrides = []
    for section in result["sections"]:
        automatic = automatic_by_id.get(section.get("id"))
        if automatic is not None and all(
            section.get(key) == automatic.get(key)
            for key in override_fields if key != "id"
        ):
            continue
        overrides.append({
            "match": list(section_key(automatic)) if automatic is not None else None,
            "section": {
                key: section.get(key) for key in override_fields
            } | {"source": "human"},
        })
    result["section_overrides"] = overrides
    result["suppressed_sections"] = sorted(suppressed)
    result["automatic_sections"] = automatic_sections
    result["revision"] = result.get("revision", 0) + 1
    result["pending_review"] = True
    invalidate_scenes(result)
    result.pop("structure_status", None)
    return result
