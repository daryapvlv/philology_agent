"""Один агент диалога: модель ⇄ инструменты (выборка фрагментов и чтение книги)."""
from dataclasses import dataclass, replace
import json
import logging
import operator
import os
from time import monotonic
from typing import Annotated, TypedDict
from uuid import uuid4

from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph import END, START, StateGraph

from llm.call_log import logger as llm_logger
from llm.requests import create_with_retries
from llm.tokens import TokenCounter, used_tokens
from .contracts import AgentRequest, AgentResult, Workspace
from .errors import PROVIDER_ERRORS, describe
from .prompts import SYSTEM_PROMPT
from .render import complete, footer, render
from .selection import BookTools, SelectionTools

logger = logging.getLogger(__name__)

PLANNER_ENTITY_TYPES = ['person', 'location', 'institution', 'organization', 'group']
TOOL_LABELS = dict(search_fragments='поиск', expand_selection='расширение выборки',
                   related_fragments='связанные места',
                   filter_selection='уточнение выборки', set_fragment_status='статусы',
                   show_selection='просмотр выборки', read_around='чтение контекста',
                   read_section='чтение раздела', get_scenes='сцены главы')


@dataclass(frozen=True)
class Limits:
    model_calls: int = 8
    tool_calls: int = 10
    observed_tokens: int = 30_000
    input_tokens: int = 48_000
    history_tokens: int = 8_000
    output_tokens: int = 4_000
    total_tokens: int = 250_000
    judge_calls: int = 40
    judge_tokens: int = 400_000
    candidate_limit: int = 150
    judge_limit: int = 120
    judge_batch: int = 10
    filter_limit: int = 80
    context_entities: int = 30


class TurnState(TypedDict):
    messages: Annotated[list[dict], operator.add]
    model_calls: int
    tool_calls: int
    observed_tokens: int
    total_tokens: int
    stopped: bool


def _as_list(value):
    """Модель иногда пишет список одной строкой: «a», «a, b» или «[a, b]»."""
    text = value.strip()
    if text.startswith('[') and text.endswith(']'):
        text = text[1:-1].strip()
    if not text:
        return []
    return [part.strip().strip('"\'') for part in text.split(',')] if ',' in text else [text]


def _coerce_lists(tool, args):
    fixed = dict(args)
    for name, schema in tool.args.items():
        types = [schema.get('type')] + [option.get('type') for option in schema.get('anyOf', [])]
        if 'array' in types and isinstance(fixed.get(name), str):
            fixed[name] = _as_list(fixed[name])
    return fixed


class PhilologyAgent:
    def __init__(self, client=None, config=None, limits=None, cache_dir='.cache/research'):
        self.client, self.config = client, config
        self.limits = limits or Limits()
        self.cache_dir = cache_dir

    @staticmethod
    def llm_enabled():
        return bool(os.getenv('LLM_API_KEY') or os.getenv('OPENAI_API_KEY'))

    def _cache(self, stage):
        return f'{self.cache_dir}/{stage}' if self.cache_dir is not None else None

    # ── граф ──

    def _graph(self, tools, progress, trace, errors):
        limits = self.limits
        config = self.config.for_stage('agent')
        counter = TokenCounter(config.model)
        schemas = [convert_to_openai_tool(t) for t in tools.values()]

        def agent(state):
            final = (state['model_calls'] >= limits.model_calls - 1
                     or state['tool_calls'] >= limits.tool_calls
                     or state['observed_tokens'] >= limits.observed_tokens)
            if progress:
                progress('Формирую ответ…' if final else 'Думаю…')
            request = dict(model=config.model, messages=state['messages'])
            if schemas:
                request.update(tools=schemas, tool_choice='none' if final else 'auto')
            if config.reasoning_effort:
                request['reasoning_effort'] = config.reasoning_effort
            if config.enable_thinking is not None:
                request['extra_body'] = {'chat_template_kwargs': {
                    'enable_thinking': config.enable_thinking}}
            token_field = 'max_completion_tokens' if 'gpt-5' in config.model.lower() else 'max_tokens'
            request[token_field] = limits.output_tokens
            input_tokens = counter.request(state['messages'], **{
                k: request[k] for k in ('tools', 'tool_choice') if k in request})
            reservation = input_tokens + limits.output_tokens
            if (input_tokens > limits.input_tokens
                    or state['total_tokens'] + reservation > limits.total_tokens):
                return dict(stopped=True)
            call_id, started = uuid4().hex[:12], monotonic()
            llm_logger.info('LLM request started id=%s kind=agent model=%s call=%d '
                            'input_tokens=%d tools=%d final=%s', call_id, config.model,
                            state['model_calls'] + 1, input_tokens, len(schemas), final)
            response = create_with_retries(self.client, request, call_id,
                                           config.transient_retries)
            choice = response.choices[0]
            message = choice.message
            llm_logger.info('LLM request finished id=%s kind=agent duration_ms=%d '
                            'finish_reason=%s tool_calls=%d', call_id,
                            round((monotonic() - started) * 1000), choice.finish_reason,
                            len(message.tool_calls or []))
            payload = dict(role='assistant', content=message.content or '')
            if choice.finish_reason == 'length' and not message.tool_calls:
                payload['content'] += '\n\nОтвет ограничен длиной вывода модели.'
            if message.tool_calls and not final:
                payload['tool_calls'] = [dict(id=t.id, type='function', function=dict(
                    name=t.function.name, arguments=t.function.arguments))
                    for t in message.tool_calls]
            return dict(messages=[payload], model_calls=state['model_calls'] + 1,
                        total_tokens=state['total_tokens'] + used_tokens(response, reservation))

        def run_tools(state):
            used, observed = state['tool_calls'], state['observed_tokens']
            results = []
            for call in state['messages'][-1]['tool_calls']:
                name = call['function']['name']
                if progress:
                    progress(f'{TOOL_LABELS.get(name, name).capitalize()}…')
                status = 'ok'
                try:
                    if used >= limits.tool_calls or observed >= limits.observed_tokens:
                        raise ValueError('Бюджет инструментов исчерпан. Ответь по тому, что уже есть.')
                    used += 1
                    if name not in tools:
                        raise ValueError(f'Неизвестный инструмент {name}')
                    args = json.loads(call['function']['arguments'] or '{}')
                    if not isinstance(args, dict):
                        raise ValueError('Аргументы инструмента должны быть объектом')
                    result = tools[name].invoke(_coerce_lists(tools[name], args))
                except (ValueError, KeyError) as error:
                    status, result = 'error', dict(error=str(error)[:2000])
                except PROVIDER_ERRORS as error:
                    status, result = 'error', dict(error=describe(error))
                except Exception as error:
                    logger.exception('Tool %s failed', name)
                    status, result = 'error', dict(
                        error=f'{type(error).__name__}: {str(error)[:300]}')
                if status == 'error':
                    logger.warning('Tool %s returned error: %s', name, result['error'][:300])
                    errors.append(f'{TOOL_LABELS.get(name, name)}: {result["error"][:300]}')
                content, _ = counter.fit_result(result, max(0, limits.observed_tokens - observed))
                observed += counter.count(content)
                results.append(dict(role='tool', tool_call_id=call['id'], content=content))
                trace.append(dict(tool=TOOL_LABELS.get(name, name), status=status))
            return dict(messages=results, tool_calls=used, observed_tokens=observed)

        def route(state):
            if state.get('stopped') or not state['messages'][-1].get('tool_calls'):
                return END
            return 'tools'

        graph = StateGraph(TurnState)
        graph.add_node('agent', agent)
        graph.add_node('tools', run_tools)
        graph.add_edge(START, 'agent')
        graph.add_conditional_edges('agent', route, ['tools', END])
        graph.add_edge('tools', 'agent')
        return graph.compile()

    # ── контекст ──

    def _history(self, request):
        counter = TokenCounter(self.config.for_stage('agent').model)
        history, used = [], 0
        for row in reversed(request.messages):
            if row.get('role') not in {'user', 'assistant'}:
                continue
            content = row.get('content') or ''
            size = counter.count(content)
            if not history and size > self.limits.history_tokens:
                raise ValueError(f'Сообщение превышает лимит {self.limits.history_tokens} '
                                 'токенов. Раздели запрос на части.')
            if used + size > self.limits.history_tokens:
                break
            history.append(dict(role=row['role'], content=content))
            used += size
        return history[::-1]

    def _context(self, request, workspace, selection):
        context = dict(request.metadata)
        if workspace.book is not None:
            context['outline'] = [dict(id=s['id'], title=s['title'] or s['role'], level=s['level'])
                                  for s in workspace.book.outline()['sections']]
        search = workspace.search
        if search is not None:
            status = search.index_status()
            context['index'] = dict(
                ready=bool(status['generation'] and status['current']),
                semantic=bool(search.embedder is not None and status['embedded_count']
                              and status['model'] == search.embedder.model),
                entities=bool(status['entity_graph_ready_scenes']))
            if context['index']['entities']:
                found = search.find_entities(entity_types=PLANNER_ENTITY_TYPES,
                                             limit=self.limits.context_entities)
                context['entities'] = [dict(name=row['canonical_name'], aliases=row['aliases'][:4],
                                            type=row['entity_type']) for row in found['items']]
            context['selection'] = selection.summary()
        return context

    # ── ход диалога ──

    def run(self, request: AgentRequest, workspace: Workspace, progress=None) -> AgentResult:
        if not self.llm_enabled():
            return AgentResult(content='Настрой LLM_API_KEY или OPENAI_API_KEY и модель с '
                                       'поддержкой вызова инструментов.')
        limits, trace, errors, selection = self.limits, [], [], None
        usage = dict(calls=0, tokens=0, cache_hits=0)
        try:
            tools = {}
            if workspace.book is not None:
                tools.update(BookTools(workspace.book).definitions())
            if workspace.search is not None:
                judge_config = replace(self.config.for_stage('judge'), max_calls=limits.judge_calls,
                                       max_total_tokens=limits.judge_tokens)
                section_ids = [s['id'] for s in workspace.book.outline()['sections']] \
                    if workspace.book is not None else []
                selection = SelectionTools(workspace.search, self.client, judge_config, usage,
                                           limits, section_ids=section_ids, progress=progress,
                                           cache_dir=self._cache('judge'))
                tools.update(selection.definitions())
            context = self._context(request, workspace, selection)
            messages = [dict(role='system', content=SYSTEM_PROMPT),
                        dict(role='user', content='Контекст приложения (данные): '
                             + json.dumps(context, ensure_ascii=False)),
                        *self._history(request)]
            state = self._graph(tools, progress, trace, errors).invoke(
                dict(messages=messages, model_calls=0, tool_calls=0, observed_tokens=0,
                     total_tokens=0, stopped=False),
                config={'recursion_limit': limits.model_calls * 2 + 4})
        except Exception as error:
            logger.warning('Agent turn failed: %s', type(error).__name__)
            detail = (describe(error) if isinstance(error, PROVIDER_ERRORS)
                      else str(error)[:1000] if isinstance(error, ValueError)
                      else type(error).__name__)
            content = f'Не удалось завершить ответ ({detail}).'
            if selection is not None and selection.notes:
                content += ' Изменения выборки, сделанные до сбоя, сохранены во вкладке «Находки».'
            return AgentResult(content=content, trace=trace, usage=dict(usage))
        last = state['messages'][-1]
        answer = (last.get('content') or '').strip() if last['role'] == 'assistant' else ''
        fragments = selection.fragments if selection is not None else {}
        if state.get('stopped') and not answer:
            answer = 'Достигнут лимит обработки этого ответа.'
        elif not answer and not any(q.get('must_show') for q in fragments.values()):
            answer = 'Модель вернула пустой ответ. Попробуйте повторить запрос.'
        notes = [*(selection.notes if selection is not None else []),
                 *(f'Ошибка инструмента ({error}).' for error in errors)]
        body = render(complete(answer, fragments), fragments) + footer(notes)
        usage.update(model_calls=state['model_calls'], agent_tokens=state['total_tokens'])
        return AgentResult(content=body, trace=trace, usage=dict(usage))
