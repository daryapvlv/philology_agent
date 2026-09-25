"""Связи между сценами: обзор сюжета по сводкам сцен.

Пара событий, которую видит классификатор narrative.links, лишена сюжета вокруг:
пересказ через три главы или «рифма ситуаций» заметны только при взгляде на
композицию. Поэтому модель получает краткий перечень всех сцен книги (outline:
номер, глава, название, первое предложение сводки) и подробно — несколько сцен
(focus), и отвечает, с какими сценами книги связаны именно они. Один большой
запрос «найди все связи между 89 сценами» хуже: середина длинного перечня
читается невнимательно, ответ длинный, а один сбой обнуляет всю карту. Запросы по
порциям сцен короткие, идут параллельно, и связь, найденная с обеих сторон,
сливается кодом. Затем пары сцен дают обязательных кандидатов классификатору
событий, который привязывает связь к местам текста (NarrativeService.rebuild_links).
Невалидная строка ответа отбрасывается и не валит остальные связи.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import json
import re

from llm.requests import json_content, request_json

from .links import BASES, DIRECTED_TYPES, SUBTYPES, NarrativeLinkError, _reason
from .scene_links_prompt import SCENE_LINK_PROMPT

SCENE_LINKS_METHOD_HASH = sha256(SCENE_LINK_PROMPT.encode()).hexdigest()[:16]
SCENE_LINK_TYPES = set(SUBTYPES)
SUMMARY_CHARS = 400


class SceneMapBudgetError(NarrativeLinkError):
    """Бюджет вызовов исчерпан — это не сбой одной порции, а остановка обзора."""
GIST_CHARS = 160
_SENTENCE = re.compile(r'(.+?[.!?…])(\s|$)')


def _clean(text):
    return ' '.join((text or '').split())


def _first_sentence(text, limit):
    text = _clean(text)
    match = _SENTENCE.match(text)
    sentence = match.group(1) if match else text
    return sentence if len(sentence) <= limit else sentence[:limit - 1].rstrip() + '…'


def outline_lines(scenes, gist_chars=GIST_CHARS):
    """Краткий перечень сцен по порядку чтения: «n | глава | название — суть»;
    gist_chars=0 — без сути, только названия."""
    lines = []
    for index, scene in enumerate(scenes, 1):
        line = f"{index} | {_clean(scene.get('chapter'))} | {_clean(scene.get('title'))}"
        gist = _first_sentence(scene.get('summary'), gist_chars) if gist_chars else ''
        lines.append(f'{line} — {gist}' if gist else line)
    return lines


def fit_outline(scenes, max_chars):
    """Перечень, который помещается в max_chars: сначала короче суть, затем
    только названия; не поместилось и так — ошибка, а не молча обрезанная книга."""
    for gist_chars in (GIST_CHARS, 80, 0):
        lines = outline_lines(scenes, gist_chars)
        if sum(len(line) + 1 for line in lines) <= max_chars:
            return lines
    raise NarrativeLinkError('Перечень сцен книги не помещается в обзор сюжета; '
                             'увеличь scene_outline_chars')


def focus_entries(scenes, numbers):
    return [dict(n=n, title=_clean(scenes[n - 1].get('title')),
                 summary=_clean(scenes[n - 1].get('summary'))[:SUMMARY_CHARS],
                 characters=list(scenes[n - 1].get('characters') or [])[:8])
            for n in numbers]


def focus_groups(count, size):
    """Номера сцен (с 1) порциями по size подряд."""
    return [list(range(start, min(start + size - 1, count) + 1))
            for start in range(1, count + 1, size)]


def max_links(focus_count):
    return max(6, 3 * focus_count)


def parse_scene_links(content, numbers, focus):
    """Строки ответа -> [{a, b, type, subtype, direction, basis, reason}] с a < b
    (более ранняя сцена первой; direction пересчитан под этот порядок). Хотя бы
    одна сторона — из focus. Невалидная строка отбрасывается; повтор пары —
    первая допустимая строка."""
    rows = content.get('links') if isinstance(content, dict) else None
    allowed, focus = set(numbers), set(focus)
    result, seen = [], set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        a, b, link_type = row.get('a'), row.get('b'), row.get('type')
        if (type(a) is not int or type(b) is not int or a == b or a not in allowed
                or b not in allowed or link_type not in SCENE_LINK_TYPES
                or not (a in focus or b in focus)):
            continue
        key = (min(a, b), max(a, b))
        if key in seen:
            continue
        seen.add(key)
        direction = row.get('direction') if link_type in DIRECTED_TYPES else None
        direction = direction if direction in ('a_to_b', 'b_to_a') else None
        if a > b and direction is not None:
            direction = 'a_to_b' if direction == 'b_to_a' else 'b_to_a'
        subtype, basis = row.get('subtype'), row.get('basis')
        result.append(dict(a=key[0], b=key[1], type=link_type,
                           subtype=subtype if subtype in SUBTYPES[link_type] else None,
                           direction=direction, basis=basis if basis in BASES else None,
                           reason=_reason(row.get('reason'))))
    return result


def _map_call(client, config, outline, scenes, numbers, cache_dir, usage):
    """Связи одной порции сцен. Оборванный или не-JSON ответ — один повтор
    (attempt=1 даёт новый ключ кэша); не помогло — NarrativeLinkError."""
    prompt = SCENE_LINK_PROMPT.replace('{max_links}', str(max_links(len(numbers))))
    payload = json.dumps(dict(outline=outline, focus=focus_entries(scenes, numbers)),
                         ensure_ascii=False)
    problem = None
    for attempt in range(2):
        raw = request_json(client, config, prompt, payload, cache_dir, usage, attempt)
        if raw is None:
            raise SceneMapBudgetError('Бюджет связей между сценами исчерпан')
        try:
            content = json_content(raw)
        except json.JSONDecodeError as error:
            problem = f'ответ не JSON: {error}'
            continue
        if content is None:
            problem = f"ответ оборван (finish_reason={raw['finish_reason']})"
            continue
        return parse_scene_links(content, range(1, len(scenes) + 1), numbers)
    raise NarrativeLinkError(f'Связи сцен {numbers[0]}–{numbers[-1]}: {problem}')


def _group_links(client, config, outline, scenes, numbers, cache_dir, usage):
    try:
        return _map_call(client, config, outline, scenes, numbers, cache_dir, usage), None
    except SceneMapBudgetError:
        raise
    except NarrativeLinkError as error:
        return [], str(error)


def map_scene_links(client, config, scenes, *, cache_dir=None, usage=None):
    """scenes по порядку чтения -> (rows, failures). rows: [{source_id, target_id,
    type, subtype, directed, basis, reason, found}]; у направленных связей source —
    пересказ/предвестие/причина; found — сколько запросов назвали пару (2 — с обеих
    сторон). failures — порции, не давшие ответа и после повтора: карта остаётся
    частичной, и это видно в результате, а не маскируется. Не удались все — ошибка."""
    usage = usage if usage is not None else {'calls': 0, 'tokens': 0, 'cache_hits': 0}
    if len(scenes) < 2:
        return [], []
    outline = fit_outline(scenes, config.scene_outline_chars)
    groups = focus_groups(len(scenes), config.scene_focus_size)
    call_config = replace(config, max_calls=max(config.max_calls, len(groups)),
                          max_input_tokens=config.scene_map_input_tokens,
                          max_output_tokens=config.scene_map_output_tokens)
    workers = max(1, min(config.link_workers, len(groups)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda numbers: _group_links(
            client, call_config, outline, scenes, numbers, cache_dir, usage), groups))
    failures = [failure for _, failure in results if failure]
    if len(failures) == len(groups):
        raise NarrativeLinkError('Обзор сюжета не удался: ' + '; '.join(failures[:3]))
    merged = {}
    for links, _ in results:
        for link in links:
            key = (link['a'], link['b'])
            if key in merged:
                merged[key]['found'] += 1
            else:
                merged[key] = dict(link, found=1)
    rows = []
    for (a, b), link in sorted(merged.items()):
        source, target = scenes[a - 1]['id'], scenes[b - 1]['id']
        if link['direction'] == 'b_to_a':
            source, target = target, source
        rows.append(dict(source_id=source, target_id=target, type=link['type'],
                         subtype=link['subtype'], directed=link['direction'] is not None,
                         basis=link['basis'], reason=link['reason'], found=link['found']))
    return rows, failures
