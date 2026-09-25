"""Общие ограничения одного запуска LLM workflow."""
from dataclasses import dataclass, field, fields, replace

# Известные стадии для for_stage/LLM_<STAGE>_MODEL (llm/client.py). 'extraction'
# сюда не входит: у неё отдельный метод for_extraction с уже устоявшимся именем
# переменной окружения (LLM_EXTRACTION_MODEL) и дефолтом reasoning_effort=low.
STAGES = ('scenes', 'narrative', 'narrative_links', 'chapter_resolution',
         'book_resolution', 'book_reconciliation', 'agent', 'judge')


@dataclass
class LLMConfig:
    model: str = "openai/gpt-oss-120b"
    max_input_chars: int = 24_000
    max_input_tokens: int = 16_000
    max_output_tokens: int = 6_000
    max_calls: int = 64
    max_total_tokens: int = 2_500_000
    transient_retries: int = 2
    # None — параметр не отправляется, провайдер использует свой уровень.
    reasoning_effort: str | None = None
    # Qwen3 и другие hybrid-модели на vLLM тратят весь max_output_tokens на
    # thinking и возвращают content=null. False отключает thinking через
    # chat_template_kwargs; None — параметр не отправляется.
    enable_thinking: bool | None = None
    # Extraction — узкая NER-подобная задача: отдельная модель и низкий reasoning.
    extraction_model: str | None = None
    extraction_reasoning_effort: str | None = "low"
    # Остальные стадии (STAGES выше): своя модель/reasoning_effort на стадию,
    # заполняется из LLM_<STAGE>_MODEL/LLM_<STAGE>_REASONING_EFFORT в
    # llm/client.py.configured_client(). Пустой словарь — все стадии используют
    # общий model/reasoning_effort, как раньше.
    stage_models: dict[str, str] = field(default_factory=dict)
    stage_reasoning_effort: dict[str, str] = field(default_factory=dict)

    def for_extraction(self):
        return replace(
            self, model=self.extraction_model or self.model,
            reasoning_effort=self.extraction_reasoning_effort or self.reasoning_effort,
            # Флаг относится к LLM_MODEL; у отдельной extraction-модели свой шаблон.
            enable_thinking=None if self.extraction_model else self.enable_thinking,
        )

    def for_stage(self, stage):
        """Конфиг для одной стадии пайплайна (см. STAGES) — своя модель, если
        задана, иначе общий model/reasoning_effort. Отсутствие переопределения
        не отличается от поведения до разделения моделей по стадиям."""
        if stage not in STAGES:
            raise ValueError(f'stage: одно из {sorted(STAGES)}')
        model = self.stage_models.get(stage)
        return replace(
            self, model=model or self.model,
            reasoning_effort=self.stage_reasoning_effort.get(stage) or self.reasoning_effort,
            # Флаг относится к общей LLM_MODEL; у отдельной модели стадии свой шаблон.
            enable_thinking=None if model else self.enable_thinking,
        )

    @classmethod
    def from_config(cls, config: "LLMConfig"):
        if isinstance(config, cls):
            return config
        if not isinstance(config, LLMConfig):
            raise TypeError("config должен быть экземпляром LLMConfig")
        common = {field.name: getattr(config, field.name) for field in fields(LLMConfig)}
        return cls(**common)
