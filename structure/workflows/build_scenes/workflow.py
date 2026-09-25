"""Сцены выбранной главы. Результат — данные, Neo4j и сюжеты здесь не нужны."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
import math
import os
from uuid import NAMESPACE_URL, uuid5

import tiktoken

from infrastructure.files import json_sha256
from llm.requests import request_json
from ...config import BuildScenesConfig
from ...rules.scenes import chapter_input, chapter_units
from ...rules.sections import HEADING_ROLES
from .prompt import PROMPT, PROMPT_STSS

SEGMENT_KINDS = {"scene", "summary", "description", "reflection"}
SEGMENT_SHIFTS = {"time", "place", "characters", "action", "mode"}


def scene_prompt():
    """Промпт разбиения: по умолчанию прежний (method_hash готовых глав не
    меняется), SCENES_METHOD=stss — определение сцены из STSS с типом сегмента."""
    return PROMPT_STSS if os.getenv("SCENES_METHOD", "").strip().casefold() == "stss" else PROMPT


def scene_batches(units, max_chars):
    """Неперекрывающиеся порции; каждый проход обязательно продвигается вперёд."""
    start = 0
    while start < len(units):
        end, size = start, 0
        while end < len(units):
            length = len(units[end]["text"]) + len(str(end)) + 4
            if size + length > max_chars:
                break
            size += length
            end += 1
        if end == start:
            raise ValueError(f"Фрагмент {start} не помещается в запрос; увеличь max_input_chars")
        yield start, end
        start = end


def next_scene_window(units, start, max_chars):
    """Окно LumberChunker: набираем последовательные единицы до лимита."""
    end, size = start, 0
    while end < len(units):
        length = len(units[end]["text"]) + len(str(end)) + 4
        if size + length > max_chars:
            break
        size += length
        end += 1
    if end == start:
        raise ValueError(f"Фрагмент {start} не помещается в запрос; увеличь max_input_chars")
    return end


def _scene_text(units, start, end):
    return "".join(unit["text"] for unit in units[start:end])


def _scene_record(units, start, end, chapter_id, input_hash, title, summary):
    first_char = units[start]["start_char"]
    text = _scene_text(units, start, end)
    return {
        "id": f"{chapter_id}:scene:{uuid5(NAMESPACE_URL, f'{chapter_id}:{input_hash}:{first_char}').hex}",
        "chapter_id": chapter_id,
        "start_char": first_char,
        "end_char": units[end - 1]["end_char"],
        "title": title.strip(),
        "summary": summary.strip(),
        "text": text,
        "text_hash": sha256(text.encode()).hexdigest(),
        "summary_hash": sha256(
            (title.strip() + '\n' + summary.strip()).encode()
        ).hexdigest(),
    }


def _vectors(values, count):
    if len(values) != count:
        raise ValueError("Провайдер вернул неверное количество эмбеддингов")
    result = [[float(x) for x in value] for value in values]
    dimensions = {len(value) for value in result}
    if len(dimensions) != 1 or not next(iter(dimensions), 0):
        raise ValueError("Эмбеддинги должны иметь одинаковую ненулевую размерность")
    if any(not all(math.isfinite(x) for x in value) or not any(value) for value in result):
        raise ValueError("Эмбеддинг содержит нечисловые значения или нулевой вектор")
    return result


def embedding_windows(text, max_tokens=None, counter=None):
    """Порезать текст сцены на окна не длиннее лимита embedding-модели.

    Сцена может занимать всю главу (у text-embedding-3-* лимит 8191 токен на
    вход, провайдер отвечает голым 400 Invalid request). Режем по строкам, а
    слишком длинную строку — по символам; короткий текст остаётся одним окном,
    поэтому эмбеддинги уже подготовленных сцен не меняются.
    """
    max_tokens = max_tokens or int(os.getenv("EMBEDDING_MAX_INPUT_TOKENS", "8000"))
    if counter is None:
        encoding = tiktoken.get_encoding(os.getenv("EMBEDDING_TOKENIZER", "cl100k_base"))
        counter = lambda value: len(encoding.encode(value, disallowed_special=()))
    if counter(text) <= max_tokens:
        return [text]
    pieces = []
    for line in text.splitlines(keepends=True):
        while counter(line) > max_tokens:
            cut = max(1, len(line) * max_tokens // counter(line) * 9 // 10)
            pieces.append(line[:cut])
            line = line[cut:]
        pieces.append(line)
    windows, current = [], ""
    for piece in pieces:
        if current and counter(current + piece) > max_tokens:
            windows.append(current)
            current = ""
        current += piece
    if current:
        windows.append(current)
    return windows


def _mean_vector(vectors, weights):
    total = sum(weights)
    return [sum(v[i] * w for v, w in zip(vectors, weights)) / total
            for i in range(len(vectors[0]))]


def _embed_texts(embedder, texts):
    """Один вызов embed_documents на все окна; вектор длинного текста —
    среднее окон, взвешенное по длине."""
    windows = [embedding_windows(text) for text in texts]
    flat = [window for parts in windows for window in parts]
    vectors = _vectors(embedder.embed_documents(flat), len(flat))
    result, position = [], 0
    for parts in windows:
        chunk = vectors[position:position + len(parts)]
        position += len(parts)
        result.append(chunk[0] if len(parts) == 1
                      else _mean_vector(chunk, [len(part) for part in parts]))
    return result


def embed_scenes(items, embedder, old=None):
    """Добавить embedding текста сцены и embedding её summary."""
    if embedder is None:
        return 0
    model = getattr(embedder, "model", "")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Укажи стабильное имя embedding-модели")
    old_by_id = {scene["id"]: scene for scene in (old or {}).get("items", [])}
    missing_text, missing_summary = [], []
    for scene in items:
        previous = old_by_id.get(scene["id"], {})
        semantic_hash = sha256((
            scene["title"].strip() + "\n" + scene["summary"].strip()
        ).encode()).hexdigest()
        current_model = scene.get("embedding_model") == model
        current_text = current_model and isinstance(scene.get("embedding"), list)
        current_summary = (scene.get("summary_embedding_model") == model
                           and scene.get("summary_embedding_hash", scene.get("summary_hash"))
                           == semantic_hash
                           and isinstance(scene.get("summary_embedding"), list))
        scene["embedding_model"] = model
        scene["summary_embedding_model"] = model
        scene["summary_hash"] = semantic_hash
        scene["summary_embedding_hash"] = semantic_hash
        if not current_text:
            if (previous.get("embedding_model") == model
                    and previous.get("text_hash") == scene["text_hash"]
                    and isinstance(previous.get("embedding"), list)):
                scene["embedding"] = previous["embedding"]
            else:
                missing_text.append(scene)
        if not current_summary:
            if (previous.get("summary_embedding_model") == model
                    and previous.get("summary_hash") == scene["summary_hash"]
                    and isinstance(previous.get("summary_embedding"), list)):
                scene["summary_embedding"] = previous["summary_embedding"]
            else:
                missing_summary.append(scene)
    embedded = 0
    if missing_text:
        vectors = _embed_texts(embedder, [scene["text"] for scene in missing_text])
        for scene, vector in zip(missing_text, vectors):
            scene["embedding"] = vector
        embedded += len(missing_text)
    if missing_summary:
        vectors = _vectors(
            embedder.embed_documents([
                scene["title"].strip() + '\n' + scene["summary"].strip()
                for scene in missing_summary
            ]),
            len(missing_summary),
        )
        for scene, vector in zip(missing_summary, vectors):
            scene["summary_embedding"] = vector
        embedded += len(missing_summary)
    return embedded


def read_boundary(reply, start, end, units, chapter_id, input_hash):
    """Прочитать ответ LumberChunker-шага: первая смена или всё окно."""
    if not isinstance(reply, dict):
        raise ValueError("Ответ модели должен быть объектом")
    if any(not isinstance(reply.get(k), str) or not reply[k].strip()
           for k in ("title", "summary")):
        raise ValueError("У сцены должны быть название и описание")
    shift = reply.get("shift_unit")
    if shift is None:
        scene_end = end
    elif type(shift) is int and start < shift < end:
        scene_end = shift
    else:
        raise ValueError("shift_unit должен указывать первую единицу следующей сцены внутри окна")
    scene = _scene_record(
        units, start, scene_end, chapter_id, input_hash,
        reply["title"], reply["summary"],
    )
    # Только в варианте STSS; неизвестное значение не отменяет границу.
    if reply.get("kind") in SEGMENT_KINDS:
        scene["segment_kind"] = reply["kind"]
    if reply.get("shift") in SEGMENT_SHIFTS and shift is not None:
        scene["boundary_shift"] = reply["shift"]
    return scene, scene_end


def read_scenes(reply, start, end, units, chapter_id, input_hash, has_previous):
    """При ошибке отклоняем всю порцию, чтобы не получить скрытые дыры в тексте."""
    if not isinstance(reply, dict) or type(reply.get("continues_previous")) is not bool:
        raise ValueError("Нужен булевый continues_previous")
    continues = reply["continues_previous"]
    if continues and not has_previous:
        raise ValueError("Первая сцена главы не может продолжать предыдущую")
    records = reply.get("scenes")
    if not isinstance(records, list) or not records:
        raise ValueError("Модель не вернула сцены")
    scenes = []
    cursor = start
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Сцена должна быть объектом")
        last = record.get("end_unit")
        if type(last) is not int or not cursor <= last < end:
            raise ValueError("Границы сцен должны возрастать внутри основной части")
        if any(not isinstance(record.get(k), str) or not record[k].strip() for k in ("title", "summary")):
            raise ValueError("У сцены должны быть название и описание")
        scenes.append(_scene_record(units, cursor, last + 1, chapter_id, input_hash,
                                    record["title"], record["summary"]))
        cursor = last + 1
    if cursor != end:
        raise ValueError("Сцены не покрывают конец основной части")
    return scenes, continues


def build_scenes(result, elements, chapter_id, client, *, config=None,
                 cache_dir=".cache/scenes", force=False, checkpoint=None,
                 replace_human=False, embedder=None):
    """Построить сцены одной главы. Актуальный результат возвращается без LLM.

    Ошибка/бюджет оставляют partial или failed. Повторный запуск восстанавливает
    успешные порции из кэша. force пересчитывает главу без кэша ответов.
    """
    config = (BuildScenesConfig.from_config(config)
              if config is not None else BuildScenesConfig())
    if (
        config.max_output_tokens <= 0
        or min(config.max_calls, config.max_total_tokens) < 0
        or min(config.request_overhead_chars, config.context_units, config.context_chars) < 0
    ):
        raise ValueError("Некорректный бюджет анализа")
    prompt = scene_prompt()
    capacity = config.max_input_chars - len(prompt) - config.request_overhead_chars
    if capacity <= 0:
        raise ValueError("Слишком маленький max_input_chars")
    updated = deepcopy(result)
    chapter = next((s for s in updated["sections"] if s["id"] == chapter_id), None)
    scene_containers = {
        "work", "part", "chapter", "paragraph", "section", "act",
        "dramatic_scene", "picture", "prologue", "epilogue",
    }
    if chapter is None or chapter["role"] not in scene_containers:
        raise ValueError("Выбери главу, раздел или произведение без глав")
    if any(s.get("parent_id") == chapter_id and s["role"] in HEADING_ROLES
           for s in updated["sections"]):
        raise ValueError("Выбери один из вложенных разделов")
    units = chapter_units(elements, chapter, updated.get("blocks", []))
    # Хеш фактических фрагментов не позволяет использовать кэш для другого текста.
    input_hash = chapter_input(updated, chapter)
    embedding_model = getattr(embedder, "model", None) if embedder is not None else None
    method_hash = json_sha256({"prompt": prompt, "model": config.model,
                            "max_input_chars": config.max_input_chars,
                            "max_output_tokens": config.max_output_tokens,
                            "endpoint": str(getattr(client, "base_url", "custom-client")),
                            "embedding_model": embedding_model,
                            "text": units})
    layers = updated.setdefault("scenes", {})
    old = layers.get(chapter_id, {})
    # Подключение или смена embedding-модели не должно повторно вызывать LLM и
    # менять уже готовые границы сцен: это отдельное обогащение того же слоя.
    if (not force and embedder is not None and old.get("status") == "ready"
            and old.get("input_hash") == input_hash):
        items = old.get("items", [])
        current = all(
            scene.get("embedding_model") == embedding_model
            and isinstance(scene.get("embedding"), list)
            and scene.get("summary_embedding_model") == embedding_model
            and scene.get("summary_embedding_hash", scene.get("summary_hash")) == sha256((
                scene["title"].strip() + "\n" + scene["summary"].strip()
            ).encode()).hexdigest()
            and isinstance(scene.get("summary_embedding"), list)
            for scene in items
        )
        if not current:
            previous = deepcopy(old)
            usage = old.setdefault("usage", {"calls": 0, "tokens": 0, "cache_hits": 0,
                                             "embedded_texts": 0})
            usage["embedded_texts"] = usage.get("embedded_texts", 0) + embed_scenes(
                items, embedder, previous,
            )
            old["method_hash"] = method_hash
            if checkpoint:
                checkpoint(updated)
            return updated
    if old.get("human_edited") and not replace_human:
        if old.get("status") == "ready" and old.get("input_hash") == input_hash and not force:
            return updated
        raise ValueError("Сцены исправлены вручную. Для замены нужен replace_human=True")
    if (not force and old.get("status") == "ready"
            and old.get("input_hash") == input_hash
            and old.get("method_hash") == method_hash
            and (embedder is None or all(
                isinstance(scene.get("embedding"), list)
                and isinstance(scene.get("summary_embedding"), list)
                for scene in old.get("items", [])
            ))):
        return updated
    if client is None:
        raise ValueError("Для построения сцен нужен клиент LLM")
    layer = {
        "status": "partial", "input_hash": input_hash, "method_hash": method_hash,
        "config": asdict(config), "items": [], "batches": [], "issues": [],
        "reviewed_units": 0, "total_units": len(units),
        "usage": {"calls": 0, "tokens": 0, "cache_hits": 0, "embedded_texts": 0},
    }
    layers[chapter_id] = layer
    try:
        start = 0
        while start < len(units):
            saved_items = deepcopy(layer["items"])
            saved_start = start
            end = next_scene_window(units, start, capacity)
            context = "Нет предыдущей сцены."
            if layer["items"]:
                context = (
                    "Предыдущая сцена: "
                    + layer["items"][-1]["summary"][-config.context_chars:]
                )
                context += "\nПоследний текст: " + "".join(
                    u["text"]
                    for u in units[max(0, start - config.context_units):start]
                )[-config.context_chars:]
            payload = f"Основная часть [{start}, {end}).\nОкно главы [{start}, {end}).\n{context}\n" + "".join(
                f"[{i}] {units[i]['text']}\n" for i in range(start, end)
            )
            raw = request_json(client, config, prompt, payload,
                               None if force else cache_dir, layer["usage"])
            layer["batches"].append({"start": start, "end": end, "response": raw})
            if raw is None:
                raise ValueError("Бюджет исчерпан; оставшийся текст не обработан")
            if raw.get("cache_error"):
                layer["issues"].append(raw["cache_error"])
            if raw["finish_reason"] != "stop":
                raise ValueError("Ответ модели оборван")
            content = json.loads(raw["content"])
            shift = content.get("shift_unit") if "scenes" not in content else None
            if ("scenes" not in content and shift is not None
                    and not (type(shift) is int and start < shift < end)):
                correction = (
                    payload
                    + "\n\nCORRECTION: предыдущий ответ вернул недопустимый "
                    + f"shift_unit={shift!r} для окна [{start}, {end}). "
                    + "Верни исправленный JSON. shift_unit должен быть null либо "
                    + f"целым числом строго между {start} и {end}."
                )
                raw = request_json(client, config, prompt, correction,
                                   None if force else cache_dir, layer["usage"])
                layer["batches"].append({"start": start, "end": end,
                                         "retry": "invalid_shift_unit",
                                         "response": raw})
                if raw is None:
                    raise ValueError("Бюджет исчерпан при исправлении shift_unit")
                if raw.get("cache_error"):
                    layer["issues"].append(raw["cache_error"])
                if raw["finish_reason"] != "stop":
                    raise ValueError("Исправленный ответ модели оборван")
                content = json.loads(raw["content"])
            if "scenes" in content:
                scenes, continues = read_scenes(content, start, end,
                                               units, chapter_id, input_hash, bool(layer["items"]))
                if continues:
                    first = scenes.pop(0)
                    layer["items"][-1].pop("embedding", None)
                    layer["items"][-1].pop("summary_embedding", None)
                    layer["items"][-1].pop("summary_embedding_model", None)
                    layer["items"][-1].pop("summary_embedding_hash", None)
                    layer["items"][-1]["end_char"] = first["end_char"]
                    layer["items"][-1]["text"] += first["text"]
                    layer["items"][-1]["text_hash"] = sha256(layer["items"][-1]["text"].encode()).hexdigest()
                    layer["items"][-1]["summary"] += "\n" + first["summary"]
                    layer["items"][-1]["summary_hash"] = sha256((
                        layer["items"][-1]["title"].strip() + "\n"
                        + layer["items"][-1]["summary"].strip()
                    ).encode()).hexdigest()
                layer["items"].extend(scenes)
                start = end
            else:
                scene, start = read_boundary(content, start, end, units, chapter_id, input_hash)
                layer["items"].append(scene)
            layer["reviewed_units"] = start
            try:
                layer["usage"]["embedded_texts"] += embed_scenes(layer["items"], embedder, old)
            except Exception:
                layer["items"] = saved_items
                layer["reviewed_units"] = saved_start
                raise
            if checkpoint:
                checkpoint(updated)
    except Exception as error:
        layer["issues"].append(f"{type(error).__name__}: {error}")
        layer["status"] = "partial" if layer["items"] else "failed"
    else:
        layer["status"] = "ready"
    if checkpoint:
        checkpoint(updated)
    return updated
