"""Сбои провайдера модели, после которых конвейер продолжает работу."""
import openai

PROVIDER_ERRORS = (openai.APIError,)


def describe(error):
    body = getattr(error, 'body', None)
    code = body.get('code') if isinstance(body, dict) else None
    if code == 'data_inspection_failed':
        return 'провайдер модели отклонил текст своим фильтром содержания'
    if isinstance(error, (openai.APIConnectionError, openai.APITimeoutError)):
        return 'нет соединения с провайдером модели'
    if isinstance(error, openai.RateLimitError):
        return 'превышен лимит запросов к провайдеру модели'
    return f'ошибка провайдера модели ({type(error).__name__})'
