"""Настройки chapter-level entity resolution."""
from dataclasses import dataclass

from llm.config import LLMConfig


@dataclass
class ChapterResolutionConfig(LLMConfig):
    top_k: int = 5
    resolution_batch_size: int = 12
    reconciliation_batch_size: int = 8
    context_chars: int = 240
    representative_contexts: int = 3


@dataclass
class BookResolutionConfig(LLMConfig):
    top_k: int = 5
    # ChapterEntity должны становиться кандидатами для следующих ChapterEntity
    # в том же запуске. Последовательная обработка устраняет false split первого
    # запуска, когда BookEntity ещё не существуют.
    batch_size: int = 1
    reconciliation_batch_size: int = 8
    representative_contexts: int = 3
