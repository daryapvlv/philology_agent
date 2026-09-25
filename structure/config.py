"""Настройки workflows структурного анализа."""
from dataclasses import dataclass

from llm.config import LLMConfig


@dataclass
class BuildScenesConfig(LLMConfig):
    """Настройки разбиения выбранной главы на сцены."""

    request_overhead_chars: int = 1_400
    context_units: int = 2
    context_chars: int = 500
