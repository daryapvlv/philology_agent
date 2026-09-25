import json
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
import openai

from agent import AgentRequest, Limits, PhilologyAgent, Workspace
from agent.judge import judge
from agent.prompts import JUDGE_PROMPT
from agent.retrieve import retrieve
from llm.config import LLMConfig
from tests.test_hybrid_search import MemorySessions, service


class RecentSessions(MemorySessions):
    """MemorySessions + история поисков, как у SQLite-хранилища."""

    def recent(self, offset=0, limit=20, current=None):
        rows = [dict(search_id=key, has_plan=bool(state.get('plan')), current=True)
                for key, state in reversed(list(self.rows.items()))]
        return rows[offset:offset + limit]


class FakeBook:
    def outline(self):
        return dict(sections=[dict(id='chapter1', title='Глава 1', role='chapter', level=2),
                              dict(id='chapter2', title='Глава 2', role='chapter', level=2)])

    def read_section(self, section_id, offset=0, limit=4000):
        return dict(section_id=section_id, text='Текст главы.')

    def scenes(self, chapter_id, offset=0, limit=10):
        return dict(chapter_id=chapter_id, items=[])


def _response(content='', tool_calls=None, finish_reason='stop'):
    return SimpleNamespace(
        usage=SimpleNamespace(total_tokens=100),
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(
            content=content, tool_calls=tool_calls))])


def call(name, **args):
    return ('call', name, args)


class FakeLLM:
    """Фальшивый провайдер: сценарий ответов агента и отдельный ответчик судьи."""
    base_url = 'test://agent'

    def __init__(self, agent_steps=(), judge=None):
        self.agent_steps = list(agent_steps)
        self.judge = judge
        self.agent_requests, self.judge_calls = [], []
        self.lock = Lock()
        self.chat = SimpleNamespace(completions=self)

    def with_options(self, **_):
        return self

    def create(self, **kwargs):
        if kwargs['messages'][0]['content'] == JUDGE_PROMPT:
            payload = json.loads(kwargs['messages'][1]['content'])
            with self.lock:
                self.judge_calls.append(payload)
            value = self.judge(payload)
            if isinstance(value, Exception):
                raise value
            return _response(json.dumps(value, ensure_ascii=False))
        self.agent_requests.append(kwargs)
        step = self.agent_steps.pop(0)
        value = step(kwargs['messages']) if callable(step) else step
        if isinstance(value, Exception):
            raise value
        if isinstance(value, str):
            return _response(value)
        calls = value if isinstance(value, list) else [value]
        return _response(tool_calls=[SimpleNamespace(id=f'c{index}', function=SimpleNamespace(
            name=name, arguments=args if isinstance(args, str)
            else json.dumps(args, ensure_ascii=False)))
            for index, (_, name, args) in enumerate(calls)])


def _candidates(payload):
    return [c for scene in payload['scenes'] for c in scene['candidates']]


def judge_by_name(payload):
    return {'verdicts': [
        dict(id=c['id'], verdict='relevant', evidence='стоял Швабрин', reason='Назван Швабрин')
        if 'Швабрин' in c['text'] else
        dict(id=c['id'], verdict='irrelevant', evidence='', reason='Швабрина нет')
        for c in _candidates(payload)]}



def workspace():
    search = service()
    search.sessions = RecentSessions()
    return Workspace(search=search, book=FakeBook())


def ask(text):
    return AgentRequest(messages=[dict(role='user', content=text)])


def run(client, space, text='где Швабрин?', **limits):
    agent = PhilologyAgent(client, LLMConfig(), limits=Limits(**limits), cache_dir=None)
    with patch.object(PhilologyAgent, 'llm_enabled', staticmethod(lambda: True)):
        return agent.run(ask(text), space)


SEARCH = call('search_fragments', criterion='в отрывке появляется Швабрин', words=['Швабрин'])


def tool_messages(client, index=-1):
    return [json.loads(m['content']) for m in client.agent_requests[index]['messages']
            if m['role'] == 'tool']


class AgentTurnTests(unittest.TestCase):
    def test_search_turn_is_three_rounds_and_renders_verified_quotes(self):
        client = FakeLLM([SEARCH, 'Швабрин у ворот.\n\n[[F1]]'], judge_by_name)
        result = run(client, workspace())
        self.assertIn('> Тут стоял Швабрин у ворот\\.', result.content)
        self.assertNotIn('Первая фраза', result.content)
        self.assertIn('подходят 1, под вопросом 0, отклонено 1', result.content)
        self.assertEqual([row['tool'] for row in result.trace], ['поиск'])
        self.assertEqual(len(client.agent_requests), 2)
        self.assertEqual(len(client.judge_calls), 1)
        fragments = tool_messages(client)[0]['fragments']
        self.assertEqual([(f['marker'], f['status']) for f in fragments], [('F1', 'relevant')])

    def test_conversation_turn_calls_no_tools(self):
        client = FakeLLM(['Могу искать фрагменты в книге.'])
        result = run(client, workspace(), 'что ты умеешь?')
        self.assertEqual(result.content, 'Могу искать фрагменты в книге.')
        self.assertEqual(client.judge_calls, [])
        names = {t['function']['name'] for t in client.agent_requests[0]['tools']}
        self.assertEqual(names, {'search_fragments', 'expand_selection', 'related_fragments',
                                 'filter_selection',
                                 'set_fragment_status', 'show_selection', 'read_around',
                                 'read_section', 'get_scenes'})

    def test_provider_rejecting_one_judge_batch_keeps_the_rest_and_says_why(self):
        rejected = openai.BadRequestError(
            'blocked', response=httpx.Response(400, request=httpx.Request('POST', 'http://x')),
            body={'code': 'data_inspection_failed'})

        def judge_reply(payload):
            return rejected if _candidates(payload)[0]['id'] == '40:70' else judge_by_name(payload)

        client = FakeLLM([SEARCH, '[[F1]]'], judge_reply)
        result = run(client, workspace(), judge_batch=1)
        self.assertIn('Тут стоял Швабрин у ворот', result.content)
        self.assertIn('Сбой проверки: 1 кандидатов не проверены: провайдер модели отклонил '
                      'текст своим фильтром содержания', result.content)
        self.assertIn('не проверено 1', result.content)


class RetrieveTests(unittest.TestCase):
    def test_missing_embeddings_are_a_visible_warning(self):
        search = service()
        search.embedder = None
        result = retrieve(search, criterion='c', words=['Швабрин'], descriptions=['Швабрин стоит у ворот крепости'])
        self.assertIsNotNone(result.search_id)
        self.assertTrue(any('эмбеддингов' in w for w in result.warnings))


if __name__ == '__main__':
    unittest.main()
