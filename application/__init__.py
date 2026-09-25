"""Сценарии приложения, связывающие UI, агента и предметные сервисы."""
from .assistant_service import AssistantService
from .book_preparation import BookPreparationService, PreparationCancelled

__all__ = ['AssistantService', 'BookPreparationService', 'PreparationCancelled']
