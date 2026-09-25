"""Общий JSON-вызов модели: бюджет и кэш сырых ответов."""
import json
import re
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from uuid import uuid4

import openai

from infrastructure.files import json_sha256, read_json, write_json
from .call_log import logger
from .tokens import TokenCounter, used_tokens


_BUDGET = Lock()
_FENCE = re.compile(r'^```(?:json)?\s*\n(.*?)\n?```\s*$', re.DOTALL)


def json_content(raw):
    """JSON из ответа модели, завершённого штатно (finish_reason=stop). Модели
    иногда оборачивают JSON в markdown-блок ```json … ``` даже при
    response_format=json_object — обёртка снимается, содержимое не правится.
    Оборванный ответ не парсится (None), невалидный JSON — JSONDecodeError."""
    if raw is None or raw.get('finish_reason') != 'stop' or not isinstance(raw.get('content'), str):
        return None
    text = raw['content'].strip()
    match = _FENCE.match(text)
    return json.loads(match.group(1) if match else text)


def request_json(client, config, prompt, payload, cache_dir, usage, attempt=0):
    reasoning_effort = getattr(config, "reasoning_effort", None)
    key_fields = {
        "prompt": prompt, "payload": payload, "model": config.model,
        "endpoint": str(getattr(client, "base_url", "custom-client")),
        "max_output_tokens": config.max_output_tokens,
    }
    if reasoning_effort:
        # Без reasoning_effort ключ прежний: существующий кэш остаётся валидным.
        key_fields["reasoning_effort"] = reasoning_effort
    enable_thinking = getattr(config, "enable_thinking", None)
    if enable_thinking is not None:
        key_fields["enable_thinking"] = enable_thinking
    if attempt:
        # Повтор того же payload не должен снова получить из кэша тот же негодный ответ.
        key_fields["attempt"] = attempt
    key = json_sha256(key_fields)
    cache_path = Path(cache_dir) / f"{key}.json" if cache_dir is not None else None
    if cache_path is not None and cache_path.exists():
        with _BUDGET:
            usage["cache_hits"] += 1
        logger.info("LLM cache hit kind=json model=%s cache_key=%s", config.model, key[:12])
        return read_json(cache_path)

    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": payload}]
    counter = TokenCounter(config.model)
    input_tokens = counter.request(messages, response_format={"type": "json_object"})
    if input_tokens > config.max_input_tokens:
        raise ValueError('Порция превышает max_input_tokens; уменьши размер порции max_input_chars')
    reservation = input_tokens + config.max_output_tokens
    with _BUDGET:
        if usage["calls"] >= config.max_calls or usage["tokens"] + reservation > config.max_total_tokens:
            return None
        usage["calls"] += 1
        usage["tokens"] += reservation

    call_id = uuid4().hex[:12]
    started = monotonic()
    logger.info(
        "LLM request started id=%s kind=json model=%s input_tokens=%d max_output_tokens=%d "
        "reasoning_effort=%s enable_thinking=%s",
        call_id, config.model, input_tokens, config.max_output_tokens, reasoning_effort,
        enable_thinking,
    )
    request = {
        "model": config.model,
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    if reasoning_effort:
        request["reasoning_effort"] = reasoning_effort
    if enable_thinking is not None:
        request["extra_body"] = {"chat_template_kwargs": {"enable_thinking": enable_thinking}}
    if "gpt-5" in config.model.lower():
        request["max_completion_tokens"] = config.max_output_tokens
    else:
        request["max_tokens"] = config.max_output_tokens
    try:
        response = create_with_retries(client, request, call_id, config.transient_retries)
    except Exception as error:
        logger.warning(
            "LLM request failed id=%s kind=json model=%s duration_ms=%d error=%s cause=%s",
            call_id, config.model, round((monotonic() - started) * 1000),
            type(error).__name__, type(error.__cause__).__name__ if error.__cause__ else None,
        )
        raise
    actual_tokens = used_tokens(response, reservation)
    with _BUDGET:
        usage["tokens"] += actual_tokens - reservation
    choice = response.choices[0]
    logger.info(
        "LLM request finished id=%s kind=json model=%s duration_ms=%d total_tokens=%d finish_reason=%s",
        call_id, config.model, round((monotonic() - started) * 1000), actual_tokens,
        choice.finish_reason,
    )
    # Сохраняем даже оборванный или невалидный JSON: оплаченный ответ не теряется.
    reply = {"content": choice.message.content, "finish_reason": choice.finish_reason}
    if cache_path is not None:
        try:
            write_json(cache_path, reply)
        except OSError as error:
            reply["cache_error"] = str(error)
    return reply


# Ошибки, при которых ответ модели не получен и повтор безопасен: обрыв
# соединения шлюзом, таймаут, перегрузка и 5xx провайдера.
TRANSIENT_ERRORS = (openai.APIConnectionError, openai.RateLimitError,
                    openai.InternalServerError)
RETRY_DELAY_SECONDS = 5


def create_with_retries(client, request, call_id, retries):
    for attempt in range(retries + 1):
        try:
            return _create(client, request, call_id)
        except TRANSIENT_ERRORS as error:
            if attempt == retries:
                raise
            delay = RETRY_DELAY_SECONDS * 2 ** attempt
            logger.warning(
                "LLM request transient failure id=%s attempt=%d/%d error=%s; retry in %ds",
                call_id, attempt + 1, retries + 1, type(error).__name__, delay,
            )
            sleep(delay)


def _create(client, request, call_id):
    completions = client.with_options(max_retries=0).chat.completions
    try:
        return completions.create(**request)
    except openai.APIStatusError as error:
        # Некоторые OpenAI-совместимые шлюзы пока принимают для GPT-5 только
        # прежнее имя параметра. Отклонённый параметр не запускает генерацию.
        message = str(error).lower()
        if "max_completion_tokens" not in request or not any(
            marker in message for marker in ("max_completion_tokens", "unknown parameter")
        ):
            raise
        logger.info("LLM request retrying with legacy token parameter id=%s", call_id)
        request["max_tokens"] = request.pop("max_completion_tokens")
        return completions.create(**request)
