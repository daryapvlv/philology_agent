"""Настройки связей: между сценами (обзор сюжета) и между событиями."""
from dataclasses import dataclass

from llm.config import LLMConfig


@dataclass
class NarrativeLinkConfig(LLMConfig):
    top_k: int = 4
    link_batch_size: int = 8
    context_chars: int = 400
    # Нижняя граница для top_k ближайших соседей по настоящему косинусу; 0.72
    # пропускал ~100 пар на книгу (у половины событий лучший сосед ниже 0.62).
    similarity_threshold: float = 0.6
    shortlist_limit_factor: float = 3.0
    participant_bonus: float = 0.05
    embedding_batch_size: int = 32
    link_workers: int = 4
    # Обзор сюжета: краткий перечень всех сцен (не длиннее scene_outline_chars) и
    # по scene_focus_size сцен подробно в одном запросе; лимиты этого запроса и
    # сколько пар событий из каждой связанной пары сцен проверять классификатором.
    scene_outline_chars: int = 30_000
    scene_focus_size: int = 6
    scene_map_input_tokens: int = 24_000
    scene_map_output_tokens: int = 8_000
    scene_anchor_pairs: int = 2
