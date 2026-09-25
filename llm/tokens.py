"""Подсчёт токенов: оценка запроса и фактический usage ответа."""
from functools import lru_cache
import json
from math import ceil
import os

import tiktoken


def serialize(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


@lru_cache(maxsize=16)
def _encoding(model, name):
    if name:
        return tiktoken.get_encoding(name)
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        raise ValueError(f'Неизвестен токенизатор модели {model}. Укажи TOKENIZER_ENCODING; '
                         'для сторонней модели это будет приближённая оценка.') from None


class TokenCounter:
    def __init__(self, model):
        self.encoding = _encoding(model, os.getenv('TOKENIZER_ENCODING'))

    def count(self, text):
        # Литералы вроде <|endoftext|> внутри книги считаются обычным текстом.
        return len(self.encoding.encode(text, disallowed_special=()))

    def request(self, messages, **options):
        # Серверное оформление сообщений/схем зависит от модели. Считаем все поля
        # отправляемого содержимого и добавляем явный запас 10% + 128 токенов.
        return ceil(self.count(serialize(dict(messages=messages, **options))) * 1.1) + 128

    def fit_result(self, result, limit):
        """JSON целиком либо валидная JSON-обёртка с префиксом, в токенном лимите."""
        content = serialize(result)
        if self.count(content) <= limit:
            return content, False
        def preview(length):
            return serialize(dict(output_truncated=True, preview=content[:length]))
        if self.count(preview(0)) > limit:
            return '', True
        left, right = 0, len(content)
        best = preview(0)
        while left <= right:
            middle = (left + right) // 2
            candidate = preview(middle)
            if self.count(candidate) <= limit:
                best, left = candidate, middle + 1
            else:
                right = middle - 1
        return best, True


def used_tokens(response, reservation):
    actual = getattr(getattr(response, 'usage', None), 'total_tokens', None)
    return actual if type(actual) is int and actual >= 0 else reservation
