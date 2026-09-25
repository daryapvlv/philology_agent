"""Сущности книги и их текстовые упоминания в сценах."""

from .service import EntityService
from .extractor import PersistentEntityExtractor
from .chapter_resolution import ChapterEntityResolver
from .book_resolution import BookEntityResolver
from .config import BookResolutionConfig, ChapterResolutionConfig

__all__ = [
    "BookEntityResolver", "BookResolutionConfig", "ChapterEntityResolver",
    "ChapterResolutionConfig", "EntityService",
    "PersistentEntityExtractor",
]
