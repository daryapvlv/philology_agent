"""Проверка кандидатов по критерию: решение и дословная выдержка от модели,
поиск выдержки в тексте и запись статуса — кодом (SearchService.apply_verdicts).
Порции проверяются параллельно; кандидат без решения остаётся непроверенным."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import os

from llm.requests import json_content, request_json

from .errors import PROVIDER_ERRORS, describe
from .prompts import JUDGE_PROMPT

STATUS = {'relevant': 'relevant', 'partial': 'uncertain', 'irrelevant': 'rejected'}


def judge_workers():
    value = int(os.getenv('JUDGE_WORKERS', '6'))
    if not 1 <= value <= 8:
        raise ValueError('JUDGE_WORKERS должен быть от 1 до 8')
    return value


@dataclass
class JudgeReport:
    quotes: list[dict] = field(default_factory=list)
    judged: int = 0
    total: int = 0
    rejected_reasons: list[str] = field(default_factory=list)
    exhausted: bool = False
    failures: list[str] = field(default_factory=list)


def _verdicts(content, batch):
    expected = {item['id'] for item in batch}
    rows = content.get('verdicts') if isinstance(content, dict) else None
    result = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get('id') not in expected or row['id'] in result:
            continue
        status = STATUS.get(row.get('verdict'))
        if status is None:
            continue
        reason = row.get('reason') if isinstance(row.get('reason'), str) else ''
        evidence = row.get('evidence') if isinstance(row.get('evidence'), str) else ''
        result[row['id']] = dict(candidate_id=row['id'], status=status,
                                 reason=reason.strip()[:500] or 'Без пояснения',
                                 evidence=evidence[:300])
    return [result[item['id']] for item in batch if item['id'] in result]


def _group_by_scene(batch, context):
    groups, order = {}, []
    for item in batch:
        ctx = context.get(item['id'], {})
        key = ctx.get('scene_id')
        if key not in groups:
            groups[key] = dict(scene_id=key, title=ctx.get('scene_title'),
                               summary=ctx.get('scene_summary'), candidates=[])
            order.append(key)
        groups[key]['candidates'].append(dict(
            id=item['id'], before=ctx.get('prev_tail') or '', text=item['text'],
            after=ctx.get('next_head') or '',
            section=' / '.join(s['title'] or s['role'] for s in item['sections'])))
    return [groups[key] for key in order]


def _ask(client, config, criterion, batch, context, cache_dir, usage, attempt):
    payload = json.dumps(dict(criterion=criterion, scenes=_group_by_scene(batch, context)),
                         ensure_ascii=False)
    raw = request_json(client, config, JUDGE_PROMPT, payload, cache_dir, usage, attempt)
    if raw is None:
        return None
    try:
        content = json_content(raw)
    except json.JSONDecodeError:
        content = None
    return _verdicts(content, batch)


def _judge_batch(client, config, criterion, batch, context, cache_dir, usage):
    """Решения по порции; пропущенным кандидатам — один повторный запрос."""
    verdicts, failure, exhausted = [], None, False
    for attempt in range(2):
        decided = {v['candidate_id'] for v in verdicts}
        pending = [item for item in batch if item['id'] not in decided]
        if not pending:
            break
        try:
            answer = _ask(client, config, criterion, pending, context, cache_dir, usage, attempt)
        except PROVIDER_ERRORS as error:
            failure = f'{len(pending)} кандидатов не проверены: {describe(error)}'
            break
        if answer is None:
            exhausted = True
            break
        verdicts += answer
    return verdicts, failure, exhausted


def judge_items(client, config, search, search_id, criterion, items, *, total=None,
                batch_size=10, cache_dir=None, usage, progress=None):
    report = JudgeReport(total=len(items) if total is None else total)
    if not items:
        return report
    context = search.context_of(search_id, [item['id'] for item in items])
    batches = [items[start:start + batch_size] for start in range(0, len(items), batch_size)]
    if progress:
        progress(f'Проверяю кандидатов: {len(items)}…')
    with ThreadPoolExecutor(max_workers=min(judge_workers(), len(batches))) as pool:
        results = list(pool.map(
            lambda batch: _judge_batch(client, config, criterion, batch, context, cache_dir, usage),
            batches))
    verdicts = []
    for batch_verdicts, failure, exhausted in results:
        verdicts += batch_verdicts
        if failure:
            report.failures.append(failure)
        report.exhausted = report.exhausted or exhausted
    report.quotes = search.apply_verdicts(search_id, verdicts)
    report.judged = len(verdicts)
    report.rejected_reasons = [v['reason'] for v in verdicts if v['status'] == 'rejected']
    return report


def judge(client, config, search, search_id, criterion, *, limit=40, batch_size=10,
          cache_dir=None, usage, progress=None):
    """Проверить следующую порцию непроверенных кандидатов поиска."""
    items, total = search.pending_candidates(search_id, limit)
    return judge_items(client, config, search, search_id, criterion, items, total=total,
                       batch_size=batch_size, cache_dir=cache_dir, usage=usage,
                       progress=progress)
