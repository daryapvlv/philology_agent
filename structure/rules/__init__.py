"""Детерминированные правила структуры книги, не зависящие от IO и LLM."""

from .scenes import (
    chapter_input, chapter_units, has_scene_text, invalidate_scenes, validate_scenes,
)
from .sections import (
    ALL_ROLES,
    BLOCK_ROLES,
    HEADING_ROLES,
    PARENT_ROLES,
    ROLES,
    apply_human_sections,
    assemble_structure,
    collect_sections,
    section_key,
    validate_section,
)
from .blocks import BLOCK_KINDS, validate_blocks
from .snapshot import validate_snapshot

__all__ = [
    "ALL_ROLES", "BLOCK_ROLES", "HEADING_ROLES", "PARENT_ROLES", "ROLES",
    "BLOCK_KINDS", "validate_blocks",
    "apply_human_sections", "assemble_structure", "chapter_input",
    "chapter_units", "collect_sections", "has_scene_text", "invalidate_scenes", "section_key",
    "validate_scenes", "validate_section", "validate_snapshot",
]
