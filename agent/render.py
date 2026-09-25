"""Подстановка подтверждённых цитат вместо маркеров [[F1]] в ответе модели."""
import re

_MARKDOWN = re.compile(r'([\\`*_{}\[\]()#+.!<>|~-])')
MARKER = re.compile(r'\[\[(F\d+)\]\]')


def render(answer, fragments):
    def replace(match):
        row = fragments.get(match.group(1))
        if row is None:
            return '[Фрагмент не подтверждён]'
        # Markdown-символы текста экранируются: книга не создаёт ссылки/HTML в UI.
        text = _MARKDOWN.sub(r'\\\1', row['text'])
        quote = '\n'.join('> ' + line for line in text.splitlines())
        titles = _MARKDOWN.sub(r'\\\1', ' / '.join(s['title'] or s['role'] for s in row['sections']))
        # Координаты и версия книги — в «Находках» и экспорте; в ответе только раздел.
        note = (' · точная выдержка не найдена, показан весь отрывок'
                if row.get('verified') is False else '')
        path = row.get('path')
        path_line = f'\n\nКак искала: {_MARKDOWN.sub(r"\\\1", path)}' if path else ''
        return f'{quote}\n\n*{titles or "Без раздела"}*{note}{path_line}'
    return MARKER.sub(replace, answer)


def complete(answer, fragments):
    """Итог выборки этого ответа одной строкой со ссылкой на «Находки»: сами
    фрагменты показывает таблица, в ответ они не дописываются."""
    shown = [q for q in fragments.values() if q.get('must_show')]
    if not shown:
        return answer.strip()
    relevant = sum(q['status'] == 'relevant' for q in shown)
    uncertain = sum(q['status'] == 'uncertain' for q in shown)
    counts = f'подходят {relevant}' + (f', под вопросом {uncertain}' if uncertain else '')
    line = f'*Найдено фрагментов: {counts}. Вся выборка — во вкладке «Находки».*'
    return '\n\n'.join(part for part in (answer.strip(), line) if part)


def footer(notes):
    notes = list(dict.fromkeys(notes))
    return '\n\n---\n*Как искала.* ' + ' '.join(notes) if notes else ''
