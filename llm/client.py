"""Конфигурация общего OpenAI-совместимого клиента приложения."""
import os

from .config import LLMConfig, STAGES


def configured_client():
    """Вернуть клиент и общую конфигурацию; клиент закрывает вызывающий код."""
    import httpx
    from openai import OpenAI

    key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key:
        raise ValueError("Задай LLM_API_KEY или OPENAI_API_KEY для анализа")
    config = LLMConfig()
    config.model = os.getenv("LLM_MODEL") or (
        config.model if os.getenv("LLM_API_KEY") else "gpt-4o-mini"
    )
    config.reasoning_effort = os.getenv("LLM_REASONING_EFFORT") or None
    thinking = (os.getenv("LLM_ENABLE_THINKING") or "").strip().casefold()
    if thinking in {"0", "false", "no", "off"}:
        config.enable_thinking = False
    elif thinking in {"1", "true", "yes", "on"}:
        config.enable_thinking = True
    elif thinking:
        raise ValueError("LLM_ENABLE_THINKING должен быть true или false")
    config.extraction_model = os.getenv("LLM_EXTRACTION_MODEL") or None
    config.extraction_reasoning_effort = (
        os.getenv("LLM_EXTRACTION_REASONING_EFFORT") or config.extraction_reasoning_effort
    )
    # Модель на стадию, например LLM_NARRATIVE_MODEL для нарративной разметки
    # или LLM_JUDGE_MODEL для проверки кандидатов — см. LLMConfig.for_stage.
    for stage in STAGES:
        env_stage = stage.upper()
        model = os.getenv(f"LLM_{env_stage}_MODEL")
        if model:
            config.stage_models[stage] = model
        effort = os.getenv(f"LLM_{env_stage}_REASONING_EFFORT")
        if effort:
            config.stage_reasoning_effort[stage] = effort
    base_url = os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    if not base_url and os.getenv("LLM_API_KEY"):
        base_url = "https://foundation-models.api.cloud.ru/v1"
    # Ответ без streaming приходит целиком после генерации; reasoning-модели на
    # лимите 24k–48k токенов генерируют несколько минут.
    timeout = httpx.Timeout(float(os.getenv("LLM_TIMEOUT_SECONDS", "600")), connect=15.0)
    client = OpenAI(api_key=key, base_url=base_url, max_retries=0, timeout=timeout)
    return client, config
