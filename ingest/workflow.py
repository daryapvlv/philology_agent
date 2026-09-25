"""Приём книги: сохранение оригинала, парсинг, финализация.

Смешивает IO (запись файлов) и пайплайн извлечения структуры. Не зависит от UI.
"""
import hashlib
from pathlib import Path
from threading import Lock
from uuid import NAMESPACE_URL, uuid5

from infrastructure.files import file_sha256, read_json, write_json
from structure.graph_db.repository import get_repository
from .parsing import (
    Document, SUPPORTED_BOOK_SUFFIXES, epigraph_blocks, epigraph_merges,
    extract_book_artifacts, note_block_id,
    note_blocks, parse_book,
)

from .migration import import_saved_book
from .repository import IngestRepository
from .artifacts import book_dir


MAX_BOOK_BYTES = 50 * 1024 * 1024
_ingest_lock = Lock()


def _merge_epub_notes(current, elements, document_id):
    existing = [block for block in current.get("blocks", [])
                if block.get("source") != "epub"]
    imported = []
    for note in note_blocks(elements, current["sections"], document_id):
        spans = [(note["start"], note["end"])]
        for block in existing:
            if block.get("section_id") != note["section_id"]:
                continue
            spans = [(left, min(right, block["start"])) for left, right in spans
                     if left < min(right, block["start"])] + [
                (max(left, block["end"]), right) for left, right in spans
                if max(left, block["end"]) < right
            ]
        for start, end in sorted(spans):
            imported.append(dict(note, start=start, end=end,
                                 id=note_block_id(document_id, note["section_id"], start, end)))
    return [*existing, *imported]


def _merge_epub_epigraph_blocks(current, elements, document_id):
    existing = current.get("blocks", [])
    imported = []
    for candidate in epigraph_blocks(elements, current["sections"], document_id):
        spans = [(candidate["start"], candidate["end"])]
        for block in existing:
            if block.get("section_id") != candidate["section_id"]:
                continue
            spans = [(left, min(right, block["start"])) for left, right in spans
                     if left < min(right, block["start"])] + [
                (max(left, block["end"]), right) for left, right in spans
                if max(left, block["end"]) < right
            ]
        for start, end in sorted(spans):
            key = f"{document_id}:epigraph:{candidate['section_id']}:{start}:{end}"
            attribution = candidate["attribution_start"]
            imported.append(dict(candidate, start=start, end=end,
                                 attribution_start=attribution if attribution is not None
                                 and start <= attribution < end else None,
                                 id=f"{document_id}:b:{uuid5(NAMESPACE_URL, key).hex}"))
    return [*existing, *imported]


def _merge_epub_epigraphs(current, elements):
    existing = [merge for merge in current.get("element_merges", [])
                if merge.get("source") != "epub"]
    imported = []
    for merge in epigraph_merges(elements):
        boundaries = {merge["start"], merge["end"]}
        for block in current.get("blocks", []):
            for boundary in (block["start"], block["end"], block.get("attribution_start")):
                if boundary is None:
                    continue
                if merge["start"] < boundary < merge["end"]:
                    boundaries.add(boundary)
        positions = sorted(boundaries)
        for start, end in zip(positions, positions[1:]):
            if end - start > 1 and not any(
                start < old["end"] and old["start"] < end for old in existing
            ):
                imported.append({"start": start, "end": end, "source": "epub"})
    return sorted([*existing, *imported], key=lambda merge: merge["start"])


def upload_info(document_id):
    if len(document_id) != 64 or any(c not in "0123456789abcdef" for c in document_id):
        raise ValueError("Некорректный идентификатор книги")
    return read_json(book_dir(document_id) / "upload.json")


def save_upload(filename: str, data: bytes) -> str:
    """Сначала сохраняем оригинал постоянно, до импорта парсера и анализа."""
    name = Path(filename.replace("\\", "/")).name
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_BOOK_SUFFIXES:
        raise ValueError("Поддерживаются EPUB и Markdown.")
    if not data or len(data) > MAX_BOOK_BYTES:
        raise ValueError("Файл должен быть непустым и не больше 50 МБ.")
    document_id = hashlib.sha256(data).hexdigest()
    destination = book_dir(document_id)
    destination.mkdir(parents=True, exist_ok=True)
    source = destination / ("original" + suffix)
    # Одинаковое содержимое имеет одинаковый ID; запись эксклюзивная.
    try:
        with source.open("xb") as output:
            output.write(data)
    except FileExistsError:
        if hashlib.sha256(source.read_bytes()).hexdigest() != document_id:
            source.write_bytes(data)
    status_path = destination / "upload.json"
    if not status_path.exists():
        ready = (destination / "structure.json").exists() and (destination / "elements.json").exists()
        write_json(status_path, {
            "name": Path(name).stem,
            "filename": source.name,
            "status": "ready" if ready else "saved",
        })
    return document_id


def process_upload(document_id):
    """Ошибки не удаляют оригинал. Незавершённую обработку можно повторить."""
    with _ingest_lock:
        info = upload_info(document_id)
        destination = book_dir(document_id)
        status_path = destination / "upload.json"
        repository = get_repository()
        if repository.exists(document_id):
            current, old_elements = repository.load(document_id)
            if (current.get("revision") == 0
                    and current.get("parser_version") != 2
                    and not current.get("blocks") and not current.get("scenes")
                    and all(section.get("source") == "parser" for section in current["sections"])):
                parsed = parse_book(Document(destination / info["filename"]))
                if [e["text"] for e in old_elements] == [e["text"] for e in parsed.elements]:
                    current["sections"] = parsed.sections
                    current["issues"] = parsed.issues
                    current["rejected_sections"] = parsed.rejected_sections
                    current["parser_version"] = 2
                    repository.save(current, 0)
                    current, old_elements = repository.load(document_id)
            if (Path(info["filename"]).suffix.lower() == ".epub"
                    and current.get("parser_version") != 6):
                parsed = parse_book(Document(destination / info["filename"]))
                if [e["text"] for e in old_elements] == [e["text"] for e in parsed.elements]:
                    current["blocks"] = _merge_epub_notes(current, parsed.elements, document_id)
                    current["blocks"] = _merge_epub_epigraph_blocks(
                        current, parsed.elements, document_id)
                    current["element_merges"] = _merge_epub_epigraphs(current, parsed.elements)
                    current["parser_version"] = 6
                    repository.save(current, current["revision"])
            info.update(status="ready", error="")
            write_json(status_path, info)
            return document_id
        if info["status"] == "ready":
            import_saved_book(destination, repository)
            return document_id
        info.update(status="processing", error="")
        write_json(status_path, info)
        try:
            source = destination / info["filename"]
            document = Document(source)
            document.elements_path = destination / "elements.json"
            parsed = extract_book_artifacts(document, output_path=document.elements_path)
            elements = read_json(document.elements_path)
            if not elements or not any(e.get("text", "").strip() for e in elements):
                raise ValueError("Не удалось извлечь текст из книги.")

            result = {
                "document_id": document_id,
                "book_title": parsed.title or info["name"],
                "revision": 0,
                "sections": parsed.sections,
                "blocks": parsed.blocks,
                "element_merges": _merge_epub_epigraphs(
                    {"blocks": parsed.blocks}, elements
                ) if source.suffix.lower() == ".epub" else [],
                "scenes": {},
                "issues": parsed.issues,
                "rejected_sections": parsed.rejected_sections,
                "reviewed_elements": len(elements),
                "usage": {},
                "section_analysis_status": "ready",
                "pending_review": True,
                "structure_profile": "prose",
                "parser_version": 6,
                "source": {
                    "elements_path": str(document.elements_path),
                    "sha256": file_sha256(document.elements_path),
                },
            }
            IngestRepository(repository).import_book(result, elements)
            info["status"] = "ready"
            write_json(status_path, info)
        except Exception as error:
            detail = str(error).strip()
            message = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
            info.update(
                status="error",
                error=f"{message}. Оригинал сохранён.",
            )
            write_json(status_path, info)
            raise
    return document_id
