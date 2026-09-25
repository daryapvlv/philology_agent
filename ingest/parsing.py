"""Извлечение текстовых элементов и оглавления из EPUB/Markdown."""
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import pathlib
import re
import tempfile
from urllib.parse import unquote, urlsplit
from uuid import NAMESPACE_URL, uuid5

from ebooklib import ITEM_DOCUMENT, epub

from infrastructure.files import file_sha256
from structure.rules import assemble_structure
from .artifacts import book_dir


SUPPORTED_BOOK_SUFFIXES = {".epub", ".md", ".markdown"}


@dataclass
class ParsedBook:
    title: str | None
    elements: list[dict]
    sections: list[dict]
    issues: list[str]
    rejected_sections: list[dict]
    blocks: list[dict] | None = None
    contents: list[tuple[int, int, str]] | None = None


class Document:
    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        self.elements_path: pathlib.Path | None = None
        self.structure_path: pathlib.Path | None = None
        self._document_id = file_sha256(self.path)

    @property
    def file_type(self) -> str | None:
        return self.path.suffix or None

    @property
    def document_id(self) -> str:
        return self._document_id

    def __repr__(self) -> str:
        return f"<Document(path={self.path!r}, document_id={self.document_id!r})>"


def _write_json(value, path: str | pathlib.Path) -> pathlib.Path:
    destination = pathlib.Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as output:
            temporary_path = pathlib.Path(output.name)
            json.dump(value, output, ensure_ascii=False, indent=2)
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return destination


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _match_key(text: str) -> str:
    return re.sub(r"[^\w]+", "", text.casefold())


class _HtmlBlocks(HTMLParser):
    BLOCK_TAGS = {
        "p", "div", "li", "blockquote", "pre", "dt", "dd",
        "td", "th", "figcaption",
    }
    SKIP_TAGS = {"script", "style", "head", "title"}
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._skip_depth = 0
        self._tag: str | None = None
        self._heading_level: int | None = None
        self._parts: list[str] = []
        self._tag_stack: list[tuple[str, bool, int | None]] = []
        self._current_note = False
        self._current_epigraph: int | None = None
        self._current_attribution = False
        self._epigraph_count = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        attributes = dict(attrs)
        semantics = (attributes.get("epub:type") or "").split()
        classes = (attributes.get("class") or "").lower().split()
        role = (attributes.get("role") or "").lower()
        is_note = tag in {"aside", "section", "div", "li", "p", "span"} and (
            any(value in {"footnote", "endnote"} for value in semantics)
            or role in {"doc-footnote", "doc-endnote"}
            or any(value in {"footnote", "endnote"} for value in classes)
        )
        if tag not in self.VOID_TAGS:
            epigraph = None
            if tag in {"blockquote", "section", "div"} and (
                "epigraph" in classes or "epigraph" in semantics or role == "doc-epigraph"
            ):
                self._epigraph_count += 1
                epigraph = self._epigraph_count
            self._tag_stack.append((tag, is_note, epigraph))
        if re.fullmatch(r"h[1-6]", tag):
            self._start_block(tag, int(tag[1]))
        elif tag in self.BLOCK_TAGS:
            self._start_block(tag, None)
        elif tag == "br" and self._tag:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if self._tag == tag:
            self._flush()
        for index in range(len(self._tag_stack) - 1, -1, -1):
            if self._tag_stack[index][0] == tag:
                del self._tag_stack[index:]
                break

    def handle_data(self, data):
        if not self._skip_depth and self._tag:
            self._parts.append(data)

    def _start_block(self, tag: str, heading_level: int | None):
        if self._tag:
            self._flush()
        self._tag = tag
        self._heading_level = heading_level
        self._parts = []
        self._current_note = any(is_note for _, is_note, _ in self._tag_stack)
        self._current_epigraph = next((group for _, _, group in reversed(self._tag_stack)
                                       if group is not None), None)
        self._current_attribution = self._current_epigraph is not None and (
            sum(tag == "blockquote" for tag, _, _ in self._tag_stack) > 1
        )

    def _flush(self):
        text = _clean_text(" ".join(self._parts))
        if text:
            element = {
                "type": "Title" if self._heading_level else "NarrativeText",
                "text": text,
            }
            if self._heading_level:
                element["metadata"] = {"heading_level": self._heading_level}
            if self._current_note:
                element.setdefault("metadata", {})["epub_note"] = True
            if self._current_epigraph is not None:
                element.setdefault("metadata", {})["epub_epigraph_group"] = self._current_epigraph
                if self._current_attribution:
                    element["metadata"]["epub_epigraph_attribution"] = True
            self.blocks.append(element)
        self._tag = None
        self._heading_level = None
        self._parts = []
        self._current_note = False
        self._current_epigraph = None
        self._current_attribution = False


def epigraph_merges(elements):
    merges = []
    start = 0
    while start < len(elements):
        group = elements[start].get("metadata", {}).get("epub_epigraph_group")
        source = elements[start].get("metadata", {}).get("source")
        end = start + 1
        if group is not None:
            while (end < len(elements)
                   and elements[end].get("metadata", {}).get("epub_epigraph_group") == group
                   and elements[end].get("metadata", {}).get("source") == source):
                end += 1
            if end - start > 1:
                merges.append({"start": start, "end": end, "source": "epub"})
        start = end
    return merges


def _parse_epub(path: pathlib.Path) -> tuple[str | None, list[dict], list[tuple[int, int, str]]]:
    elements: list[dict] = []
    try:
        book = epub.read_epub(str(path))
        titles = book.get_metadata("DC", "title")
        title = titles[0][0] if titles else None
        by_source = {}
        for item_id, _ in book.spine:
            item = book.get_item_with_id(item_id)
            if item is None or item.get_type() != ITEM_DOCUMENT:
                continue
            source = item.get_name()
            parser = _HtmlBlocks()
            parser.feed(item.get_content().decode("utf-8", errors="replace"))
            for element in parser.blocks:
                element.setdefault("metadata", {})["source"] = source
                by_source.setdefault(source, []).append(len(elements))
                elements.append(element)

        contents = []
        last_position = -1
        def visit(entries, depth=0):
            nonlocal last_position
            for entry in entries:
                node, children = entry if isinstance(entry, tuple) else (entry, ())
                href = getattr(node, "href", None)
                label = _clean_text(getattr(node, "title", "") or "")
                if href and label:
                    source = unquote(urlsplit(href).path)
                    candidates = by_source.get(source, [])
                    normalized = _match_key(label)
                    position = next((i for i in candidates if i > last_position
                                     and _match_key(elements[i]["text"]) == normalized), None)
                    if position is None:
                        position = next((i for i in candidates if i > last_position
                                         and elements[i]["type"] == "Title"
                                         and normalized in _match_key(elements[i]["text"])), None)
                    if position is None:
                        position = next((i for i in candidates if i > last_position
                                         and elements[i]["type"] == "Title"), None)
                    if position is None:
                        position = next((i for i in candidates if i > last_position), None)
                    if position is not None:
                        contents.append((position, depth, elements[position]["text"]))
                        last_position = position
                visit(children, depth + 1)
        visit(book.toc)
        note_sources = {
            unquote(urlsplit(getattr(node, "href", "") or "").path)
            for entry in book.toc
            for node in _toc_nodes(entry)
            if re.fullmatch(r"(?:notes?|endnotes?|footnotes?|примечания|сноски|комментарии)",
                            _clean_text(getattr(node, "title", "") or "").casefold())
        }
        for element in elements:
            source = element.get("metadata", {}).get("source", "")
            basename = pathlib.PurePosixPath(source).name.casefold()
            if source in note_sources or re.fullmatch(
                r"(?:content)?(?:footnotes|endnotes|notes)\d*\.x?html", basename,
            ):
                element["metadata"]["epub_note"] = True
    except (OSError, KeyError, ValueError) as error:
        raise ValueError("Не удалось прочитать EPUB") from error
    return title, elements, contents


def _toc_nodes(entry):
    node, children = entry if isinstance(entry, tuple) else (entry, ())
    yield node
    for child in children:
        yield from _toc_nodes(child)


def note_block_id(document_id, section_id, start, end):
    key = f"{document_id}:footnotes:{section_id}:{start}:{end}"
    return f"{document_id}:b:{uuid5(NAMESPACE_URL, key).hex}"


def epigraph_blocks(elements, sections, document_id):
    blocks = []
    active = None
    for position, element in enumerate(elements):
        metadata = element.get("metadata", {})
        group = metadata.get("epub_epigraph_group")
        owners = [section for section in sections
                  if section["start"] <= position < section["end"]
                  and section.get("title_position") != position]
        owner = max(owners, key=lambda section: section["level"], default=None)
        if group is None or owner is None or metadata.get("epub_note"):
            active = None
            continue
        key = (metadata.get("source"), group, owner["id"])
        if active is not None and active[0] == key and active[1]["end"] == position:
            block = active[1]
            block["end"] = position + 1
        else:
            block = {"section_id": owner["id"], "role": "epigraph",
                     "start": position, "end": position + 1,
                     "attribution_start": None, "source": "epub"}
            blocks.append(block)
            active = (key, block)
        if metadata.get("epub_epigraph_attribution") and block["attribution_start"] is None:
            block["attribution_start"] = position
    for block in blocks:
        key = f"{document_id}:epigraph:{block['section_id']}:{block['start']}:{block['end']}"
        block["id"] = f"{document_id}:b:{uuid5(NAMESPACE_URL, key).hex}"
    return blocks


def note_blocks(elements, sections, document_id):
    blocks = []
    active = None
    for position, element in enumerate(elements):
        owners = [section for section in sections
                  if section["start"] <= position < section["end"]
                  and section.get("title_position") != position]
        owner = max(owners, key=lambda section: section["level"], default=None)
        note = element.get("metadata", {}).get("epub_note") and owner is not None
        if note and active is not None and active["section_id"] == owner["id"] and active["end"] == position:
            active["end"] = position + 1
        elif note:
            active = {"section_id": owner["id"], "role": "footnotes",
                      "start": position, "end": position + 1, "source": "epub"}
            blocks.append(active)
        else:
            active = None
    for block in blocks:
        block["id"] = note_block_id(document_id, block["section_id"],
                                    block["start"], block["end"])
    return blocks


def _parse_markdown(path: pathlib.Path) -> tuple[str | None, list[dict]]:
    elements: list[dict] = []
    paragraph: list[str] = []
    in_fence = False

    def flush_paragraph():
        if paragraph:
            text = _clean_text(" ".join(paragraph))
            if text:
                elements.append({"type": "NarrativeText", "text": text})
            paragraph.clear()

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            paragraph.append(line)
            continue
        heading = None if in_fence else re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            flush_paragraph()
            text = _clean_text(heading.group(2))
            elements.append({
                "type": "Title",
                "text": text,
                "metadata": {"heading_level": len(heading.group(1))},
            })
        elif line.strip():
            paragraph.append(line.strip())
        else:
            flush_paragraph()
    flush_paragraph()
    title = next((item["text"] for item in elements
                  if item.get("metadata", {}).get("heading_level") == 1), None)
    return title, elements


def _role_for_depth(depth: int) -> str:
    if depth <= 1:
        return "chapter"
    if depth == 2:
        return "section"
    return "paragraph"


def _sections_from_contents(contents, elements, document_id):
    issues, rejected = [], []
    root_with_children = [i for i, (_, depth, _) in enumerate(contents[:-1])
                          if depth == 0 and contents[i + 1][1] > depth]
    book_root_index = 0 if root_with_children == [0] else None
    has_works = book_root_index is not None and any(depth > 1 for _, depth, _ in contents)
    proposals = []
    for index, (position, depth, title) in enumerate(contents):
        if index == book_root_index:
            continue
        has_children = index + 1 < len(contents) and contents[index + 1][1] > depth
        if has_works and depth == 1 and has_children:
            role = "work"
        elif book_root_index is None and depth == 0 and has_children:
            role = "work"
        else:
            relative_depth = depth - int(book_root_index is not None)
            if has_works and depth > 1:
                relative_depth -= 1
            role = _role_for_depth(max(1, relative_depth + 1))
        level = max(1, depth + 1 - int(book_root_index is not None))
        proposals.append({"title": title, "title_position": position, "start": position,
                          "end": None, "level": level, "role": role, "source": "epub_toc"})
    return assemble_structure(proposals, elements, document_id, issues, rejected), issues, rejected


def _sections_from_headings(elements: list[dict], document_id: str) -> tuple[list[dict], list[str], list[dict]]:
    issues: list[str] = []
    rejected: list[dict] = []
    headings = [
        (position, element["metadata"]["heading_level"], element["text"])
        for position, element in enumerate(elements)
        if isinstance(element.get("metadata", {}).get("heading_level"), int)
    ]
    if not headings:
        proposals = [{
            "title": None, "title_position": None, "start": 0, "end": len(elements),
            "level": 1, "role": "work", "source": "parser",
        }]
        return assemble_structure(proposals, elements, document_id, issues, rejected), issues, rejected

    min_heading = min(level for _, level, _ in headings)
    top_count = sum(1 for _, level, _ in headings if level == min_heading)
    has_lower_headings = any(level > min_heading for _, level, _ in headings)
    multi_work = top_count > 1
    keep_single_work = top_count == 1 and not has_lower_headings
    proposals = []
    for position, heading_level, title in headings:
        if multi_work and heading_level == min_heading:
            role = "work"
            level = 1
        elif not multi_work and heading_level == min_heading and not keep_single_work:
            continue
        elif keep_single_work and heading_level == min_heading:
            role = "work"
            level = 1
        else:
            depth = heading_level - min_heading
            role = _role_for_depth(depth)
            level = depth if not multi_work else depth + 1
        proposals.append({
            "title": title,
            "title_position": position,
            "start": position,
            "end": None,
            "level": level,
            "role": role,
            "source": "parser",
        })
    if not proposals:
        proposals.append({
            "title": None, "title_position": None, "start": 0, "end": len(elements),
            "level": 1, "role": "work", "source": "parser",
        })
    sections = assemble_structure(proposals, elements, document_id, issues, rejected)
    return sections, issues, rejected


def parse_book(document: Document) -> ParsedBook:
    suffix = document.path.suffix.lower()
    if suffix == ".epub":
        title, elements, contents = _parse_epub(document.path)
    elif suffix in {".md", ".markdown"}:
        title, elements = _parse_markdown(document.path)
        contents = []
    else:
        raise ValueError("Поддерживаются EPUB и Markdown.")
    elements = [element for element in elements if element.get("text", "").strip()]
    if not elements:
        raise ValueError("Не удалось извлечь текст из книги.")
    sections, issues, rejected = (
        _sections_from_contents(contents, elements, document.document_id)
        if contents else _sections_from_headings(elements, document.document_id)
    )
    blocks = (note_blocks(elements, sections, document.document_id)
              + epigraph_blocks(elements, sections, document.document_id)
              if suffix == ".epub" else [])
    return ParsedBook(title=title, elements=elements, sections=sections,
                      issues=issues, rejected_sections=rejected, blocks=blocks,
                      contents=contents)


def extract_book_artifacts(
    document: Document,
    output_path: str | pathlib.Path | None = None,
) -> ParsedBook:
    destination = (
        pathlib.Path(output_path) if output_path is not None
        else document.elements_path if document.elements_path is not None
        else book_dir(document.document_id) / "elements.json"
    )
    destination = destination.resolve()
    if destination == document.path.resolve() or (
        destination.exists() and destination.samefile(document.path)
    ):
        raise ValueError("Путь для JSON не должен совпадать с исходным файлом")
    parsed = parse_book(document)
    document.elements_path = _write_json(parsed.elements, destination)
    document.structure_path = None
    return parsed
