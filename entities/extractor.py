"""LLM extraction устойчивых сущностей из одной сцены."""
from dataclasses import replace
import json

from llm.config import LLMConfig
from llm.requests import request_json

from .prompt import EXTRACTION_PROMPT
from .workflow import normalize_mentions, normalize_references


class PersistentEntityExtractor:
    def __init__(self, client, *, config=None,
                 cache_dir=".cache/entities/extraction"):
        self.client = client
        base = LLMConfig.from_config(config) if config is not None else LLMConfig()
        self.config = base.for_extraction()
        self.cache_dir = cache_dir

    def extract(self, scene, *, chunks=None):
        if not isinstance(scene, dict) or not isinstance(scene.get("text"), str):
            raise ValueError("Extractor ожидает Scene с полем text")
        usage = {"calls": 0, "tokens": 0, "cache_hits": 0}
        portions = self._portions(scene, chunks)
        all_mentions, all_references = [], []
        rejected_mentions = rejected_references = 0
        for index, portion in enumerate(portions):
            payload = json.dumps({
                "scene_id": scene.get("id"),
                "chunk_id": portion.get("id"),
                "scene_text": portion["text"],
            }, ensure_ascii=False)
            raw = self._request_complete(payload, usage)
            if raw is None:
                raise ValueError("Бюджет extraction исчерпан")
            if raw["finish_reason"] != "stop":
                raise ValueError(
                    f"Ответ extraction для TextChunk {index + 1}/{len(portions)} "
                    f"оборван после автоматических повторов "
                    f"(finish_reason={raw['finish_reason']})"
                )
            content = json.loads(raw["content"])
            local_scene = {**scene, "text": portion["text"]}
            mentions, mention_rejections = normalize_mentions(
                content, scene=local_scene,
            )
            references, reference_rejections = normalize_references(
                content, scene=local_scene,
                anchor_local_ids={mention["local_id"] for mention in mentions},
                anchor_spans={(mention["start_offset"], mention["end_offset"])
                              for mention in mentions},
            )
            prefix = f"chunk{index + 1}:"
            base = portion["scene_offset"]
            for mention in mentions:
                mention["local_id"] = prefix + mention["local_id"]
                mention["start_offset"] += base
                mention["end_offset"] += base
            for reference in references:
                reference["target_local_id"] = prefix + reference["target_local_id"]
                reference["start_offset"] += base
                reference["end_offset"] += base
            all_mentions.extend(mentions)
            all_references.extend(references)
            rejected_mentions += mention_rejections
            rejected_references += reference_rejections
        return {
            "mentions": sorted(all_mentions,
                               key=lambda item: (item["start_offset"], item["end_offset"])),
            "references": sorted(all_references,
                                 key=lambda item: (item["start_offset"], item["end_offset"])),
            "rejected_mentions": rejected_mentions,
            "rejected_references": rejected_references,
            "usage": usage,
        }

    def _request_complete(self, payload, usage):
        """Повторить reasoning-вызов с большим completion budget при `length`.

        gpt-oss может потратить исходные 6000 токенов на reasoning и вернуть
        content=null даже для короткого TextChunk. Увеличение лимита меняет ключ
        сырого кэша, поэтому сохранённый оборванный ответ не зацикливает retry.
        """
        config = self.config
        # У gpt-oss completion включает reasoning. На 6000 токенах Cloud.ru
        # периодически возвращает content=null ещё до начала JSON.
        base_limit = config.max_output_tokens
        if "gpt-oss" in config.model.casefold():
            base_limit = max(base_limit, 12_000)
        raw = None
        for multiplier in (1, 2, 4):
            limit = base_limit * multiplier
            attempt = (config if limit == config.max_output_tokens else
                       replace(config, max_output_tokens=limit))
            raw = request_json(
                self.client, attempt, EXTRACTION_PROMPT,
                payload, self.cache_dir, usage,
            )
            if raw is None or raw.get("finish_reason") != "length":
                return raw
        return raw

    @staticmethod
    def _portions(scene, chunks):
        """Привести абсолютные координаты TextChunk к локальным координатам сцены."""
        if chunks is None:
            return [{"id": scene.get("id"), "text": scene["text"], "scene_offset": 0}]
        scene_start = scene.get("start_char")
        if type(scene_start) is not int:
            raise ValueError("Для chunk extraction у Scene нужен start_char")
        portions = []
        for chunk in chunks:
            start, end = chunk.get("start_char"), chunk.get("end_char")
            text = chunk.get("text")
            offset = start - scene_start if type(start) is int else None
            if (type(start) is not int or type(end) is not int
                    or not isinstance(text, str) or not text
                    or offset < 0 or offset + len(text) > len(scene["text"])
                    or end - start != len(text)
                    or scene["text"][offset:offset + len(text)] != text):
                raise ValueError("TextChunk не соответствует границам текущей сцены")
            portions.append({**chunk, "scene_offset": offset})
        return portions
