"""Проверка блоков, не участвующих в дереве разделов."""


BLOCK_KINDS = frozenset({"epigraph", "footnotes"})


def validate_blocks(result, elements):
    sections = {section["id"]: section for section in result["sections"]}
    ids = set()
    ranges = {}
    for block in result.get("blocks", []):
        block_id = block.get("id")
        if not isinstance(block_id, str) or not block_id or block_id in ids:
            raise ValueError("ID блоков должны быть непустыми и уникальными")
        ids.add(block_id)
        if block.get("role") not in BLOCK_KINDS:
            raise ValueError("Неизвестная роль литературного блока")
        start, end = block.get("start"), block.get("end")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(elements):
            raise ValueError("Некорректные границы литературного блока")
        section_id = block.get("section_id")
        if section_id is not None:
            section = sections.get(section_id)
            if section is None or section["start"] > start or (
                section["end"] is not None and end > section["end"]
            ):
                raise ValueError("Литературный блок выходит за границы раздела")
            ranges.setdefault(section_id, []).append((start, end))
        attribution = block.get("attribution_start")
        if attribution is not None and (
            type(attribution) is not int or not start <= attribution < end
        ):
            raise ValueError("Некорректная атрибуция эпиграфа")
    for section_ranges in ranges.values():
        ordered = sorted(section_ranges)
        if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
            raise ValueError("Эпиграфы и примечания одного раздела пересекаются")
