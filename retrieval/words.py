"""Подготовка обычного текста для полнотекстового поиска."""
import re


def lexical_query(text):
    terms = list(dict.fromkeys(re.findall(r'[^\W_]+', text, re.UNICODE)))
    if not terms:
        raise ValueError('Запрос должен содержать слова или числа')
    return ' OR '.join(f'"{term}"' for term in terms)
