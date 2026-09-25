import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
import openai

from llm.config import LLMConfig
from llm.requests import request_json


def _response(content):
    return SimpleNamespace(
        usage=SimpleNamespace(total_tokens=50),
        choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content=content),
        )],
    )


class FlakyClient:
    base_url = "test://llm"

    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0
        self.chat = SimpleNamespace(completions=self)

    def with_options(self, **_):
        return self

    def create(self, **_):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return _response(json.dumps({"ok": True}))


def _connection_error():
    return openai.APIConnectionError(request=httpx.Request("POST", "test://llm"))


def _usage():
    return {"calls": 0, "tokens": 0, "cache_hits": 0}


@patch("llm.requests.sleep", lambda _seconds: None)
class RequestJsonRetryTests(unittest.TestCase):
    def test_connection_drop_is_retried_and_budget_reserved_once(self):
        client, usage = FlakyClient([_connection_error()]), _usage()
        raw = request_json(client, LLMConfig(), "prompt", "payload", None, usage)
        self.assertEqual(json.loads(raw["content"]), {"ok": True})
        self.assertEqual(client.calls, 2)
        self.assertEqual(usage["calls"], 1)

class RequestJsonReasoningTests(unittest.TestCase):
    def test_enable_thinking_changes_cache_key(self):
        from tempfile import TemporaryDirectory
        client = FlakyClient([])
        with TemporaryDirectory() as cache:
            request_json(client, LLMConfig(), "p", "x", cache, _usage())
            usage = _usage()
            request_json(client, LLMConfig(enable_thinking=False), "p", "x", cache, usage)
            self.assertEqual(usage["cache_hits"], 0)

class ForStageTests(unittest.TestCase):
    def test_stage_override_replaces_model_and_reasoning_effort_only_for_that_stage(self):
        config = LLMConfig(model="base-model", reasoning_effort="medium",
                           stage_models={"judge": "judge-model"},
                           stage_reasoning_effort={"judge": "high"})
        judge_config = config.for_stage("judge")
        self.assertEqual(judge_config.model, "judge-model")
        self.assertEqual(judge_config.reasoning_effort, "high")
        agent_config = config.for_stage("agent")
        self.assertEqual(agent_config.model, "base-model")
        self.assertEqual(agent_config.reasoning_effort, "medium")

if __name__ == "__main__":
    unittest.main()
