"""Состояние экрана: связывает UI с Book, Chat, PhilologyAgent."""
import asyncio
from contextlib import contextmanager
import sqlite3
from threading import Event, Lock
from uuid import uuid4

import reflex as rx
from neo4j.exceptions import Neo4jError, DriverError

from agent import PhilologyAgent
from application import AssistantService, BookPreparationService, PreparationCancelled
from application.book_preparation import describe_error
from chat import Chat
from infrastructure.sqlite import get_chat_repository
from narrative import narrative_layer_enabled
from .book import Book
from .labels import (
    chapter_table, checklist, entity_type_label, progress_row, progress_text, readable_error,
    split_answer, technical_details,
)
from .search import Findings, STATUS_LABELS, STATUS_ORDER, export_filename
from ingest import (
    MAX_BOOK_BYTES, save_upload, process_upload, upload_info,
)
from ingest.artifacts import book_dir
from llm.call_log import logger
from llm import configured_client
from retrieval.embeddings import configured_embedder
from structure.graph_db import get_repository


ELEMENT_PAGE_SIZE = 120

_PREPARATION_CANCELLATIONS = {}
_PREPARATION_CANCELLATIONS_LOCK = Lock()


def _with_llm(action):
    """Выполнить action(client, config) в потоке и закрыть клиент после вызова."""
    client, config = configured_client()
    with client:
        return action(client, config)


SCROLL_BOOK_TO_TOP_JS = """
requestAnimationFrame(() => requestAnimationFrame(() => {
  const reader = document.getElementById('book-reader-scroll');
  if (reader) reader.scrollTo({top: 0, left: 0, behavior: 'auto'});
}));
"""

def _highlight_review_block_js(start: int, end: int, block_type: str) -> str:
    target_id = "book-chapter-heading" if block_type == "heading" else f"book-element-{start}"
    return f"""
requestAnimationFrame(() => requestAnimationFrame(() => {{
  const reader = document.getElementById('book-reader-scroll');
  document.querySelectorAll('.review-target').forEach((node) =>
    node.classList.remove('review-target'));
  const root = document.getElementById('book-selection-reader');
  const nodes = root ? Array.from(root.querySelectorAll('[data-book-element]')) : [];
  const targets = nodes.filter((node) => {{
    const left = Number(node.dataset.bookElement);
    const right = Number(node.dataset.bookElementEnd) || left + 1;
    return left < {end} && {start} < right;
  }});
  targets.forEach((node) => node.classList.add('review-target'));
  const target = document.getElementById('{target_id}') || targets[0];
  if (!target || !reader) return;
  target.classList.add('review-target');
  const readerRect = reader.getBoundingClientRect();
  const targetRect = target.getBoundingClientRect();
  reader.scrollTo({{
    top: reader.scrollTop + targetRect.top - readerRect.top - reader.clientHeight * 0.22,
    behavior: 'smooth',
  }});
  window.setTimeout(() => document.querySelectorAll('.review-target').forEach((node) =>
    node.classList.remove('review-target')), 3200);
}}));
"""


def _chats():
    return get_chat_repository()


def _view_messages(messages):
    """Сообщения для отображения: ответ и свёрнутый подвал «Как искала» отдельно.

    Хранимый формат не меняется — split_answer раскладывает и старые чаты."""
    view = []
    for message in messages:
        row = {key: str(value) for key, value in dict(message).items()}
        body, footer = split_answer(row.get("content", ""))
        row.update(body=body, footer=footer, tools=row.get("tools", ""))
        view.append(row)
    return view


class State(rx.State):
    # ═══ Сайдбар ═════════════════════════════════════════════════
    documents: list[dict[str, str]] = []
    chats: list[dict[str, str]] = []
    active_chat: str = rx.LocalStorage("", name="philology.active_chat")
    choosing_book: bool = False
    sidebar_hidden: bool = False
    delete_dialog_open: bool = False
    delete_chat_id: str = ""
    delete_chat_title: str = ""

    # ═══ Загрузка книги ═════════════════════════════════════════
    upload_status: str = ""
    processing_book: bool = False
    pending_upload: str = rx.LocalStorage("", name="philology.pending_upload")

    # ═══ Текущий чат ════════════════════════════════════════════
    document_id: str = ""
    book: str = ""
    chat_title: str = ""
    messages: list[dict[str, str]] = []
    tab: str = "chat"
    user_prompt: str = ""

    # ═══ Структура ══════════════════════════════════════════════
    sections: list[dict[str, str]] = []
    chapters: list[dict[str, str]] = []
    selected: str = "0"
    excerpt: str = ""
    epigraph_items: list[dict[str, str]] = []
    show_element_numbers: bool = False
    selectable_elements: list[dict[str, str]] = []
    selectable_total: int = 0
    element_page_start: int = 0
    element_page_label: str = ""
    can_previous_elements: bool = False
    can_next_elements: bool = False
    selection_error: str = ""
    selection_notice: str = ""
    selection_notice_token: int = 0
    selection_active: bool = False
    selection_start: int = -1
    selection_end: int = -1
    selected_blocks: list[str] = []
    selection_left: str = "16px"
    selection_top: str = "16px"
    text_context_menu_open: bool = False
    section_editor_open: bool = False
    section_editor_mode: str = "edit"
    section_edit_title: str = ""
    section_edit_role: str = "chapter"
    section_edit_start: str = ""
    section_edit_end: str = ""
    section_edit_title_position: str = ""
    section_edit_level: str = "1"
    section_edit_start_preview: str = ""
    section_edit_end_preview: str = ""
    pending_review: bool = True
    can_undo_structure: bool = False
    undo_structure_label: str = ""
    structure_status: str = "needs_review"
    structure_issues: str = ""
    review_issues: list[str] = []
    review_targets: list[dict[str, str]] = []
    can_previous_section: bool = False
    can_next_section: bool = False
    scene_items: list[dict[str, str]] = []
    scene_status: str = "Сцены ещё не построены"
    scene_issues: str = ""
    scene_editor_open: bool = False
    scene_id: str = ""
    scene_title: str = ""
    scene_summary: str = ""
    scene_range: str = ""
    scene_has_manual: bool = False
    replace_human_scenes: bool = False
    scenes_ready: bool = False
    entity_status: str = "Персонажи и места ещё не найдены"
    entity_items: list[dict[str, str]] = []
    book_entity_status: str = "Персонажи и места книги ещё не собраны"
    book_entity_items: list[dict[str, str]] = []
    preparation_scope: str = "chapter"
    preparation_embeddings: bool = True
    preparation_status: str = ""
    preparation_details: str = ""
    # Живой чек-лист подготовки: key/label/count/state (done|todo|running|error).
    preparation_rows: list[dict[str, str]] = []
    preparation_progress: str = ""
    preparation_running: str = ""
    preparation_failed: str = ""
    preparation_error: str = ""
    preparation_error_detail: str = ""
    preparation_summary: str = ""
    preparation_failed_chapters: list[str] = []
    preparation_failures: list[dict[str, str]] = []
    preparation_active: bool = False
    preparation_cancelling: bool = False
    preparation_cancel_token: str = ""
    selected_chapter_title: str = ""

    # ═══ Находки ════════════════════════════════════════════════
    findings_searches: list[dict[str, str]] = []
    findings_selected_search: str = ""
    findings_query: str = ""
    findings_items: list[dict[str, str]] = []
    findings_next_offset: str = ""
    findings_include_all: bool = False
    findings_status: str = ""
    findings_filter: str = ""
    findings_counts: list[dict[str, str]] = []
    findings_list_hidden: bool = False

    # ═══ Редактирование разделов ═══════════════════════════
    title: str = ""


    # ═══ Статусы ════════════════════════════════════════════════
    busy: bool = False
    agent_status: str = ""
    llm_enabled: bool = False
    error: str = ""
    database_error: str = ""

    # ═══ Backend ════════════════════════════════════════════════
    _book: Book | None = None
    _chat: Chat | None = None


    def __getstate__(self):
        """Не сохранять живые backend-объекты в состоянии Reflex.

        `_book` содержит Neo4j Driver через BookService, а `_chat` содержит
        runtime-состояние чата. Оба объекта нужны только текущему backend-
        запросу и не должны попадать в pickle Reflex.
        """
        state = self.__dict__.copy()
        backend = state.get("_backend_vars")
        if backend is not None:
            backend = dict(backend)
            backend.pop("_book", None)
            backend.pop("_chat", None)
            state["_backend_vars"] = backend
        state.pop("_book", None)
        state.pop("_chat", None)
        return state

    # ═══ UI-сеттеры ═════════════════════════════════════════════

    @rx.event
    def set_user_prompt(self, value: str):
        self.user_prompt = value

    @rx.event
    def set_tab(self, value: str):
        if value in {"structure", "graph", "findings"} and self._book is None:
            return
        self.tab = value
        if value == "structure":
            return rx.call_script(SCROLL_BOOK_TO_TOP_JS)
        if value == "findings":
            self._load_findings()

    @rx.event
    def set_choosing_book(self, value: bool):
        self.choosing_book = value

    @rx.event
    def clear_error(self):
        self.error = ""

    # ═══ Хелперы блокировки и ошибок ════════════════════════════

    def _can_proceed(
        self,
        *,
        need_session: bool = False,
        forbid_document: bool = False,
    ) -> bool:
        if self.busy or self.processing_book:
            return False
        if need_session and (self._book is None or self._chat is None):
            return False
        if forbid_document and self.document_id:
            return False
        return True

    @contextmanager
    def _handle_errors(self):
        try:
            yield
            self.error = ""
        except (ValueError, OSError, KeyError, sqlite3.Error, Neo4jError, DriverError) as error:
            self.error = str(error)
            if self._book is not None:
                self._sync_structure()

    def _notice_with_autohide(self, text: str):
        """Установить notice и вернуть call_script, гасящий его через 2с."""
        self.selection_notice_token += 1
        self.selection_notice = text
        token = self.selection_notice_token
        return rx.call_script(
            f"new Promise(resolve => setTimeout(() => resolve({token}), 2000))",
            callback=State.clear_selection_notice,
        )

    # ═══ Синхронизация ══════════════════════════════════════════

    def _sync(self):
        self._sync_header()
        self._sync_messages()
        self._sync_structure()

    def _sync_header(self):
        self.book = self._book.title if self._book else "Без книги"
        self.chat_title = self._chat.title
        self.chats = self._list_chats()
        self.llm_enabled = PhilologyAgent.llm_enabled()
        self.pending_review = self._book.pending_review if self._book else False

    def _sync_messages(self):
        self.messages = _view_messages(self._chat.messages)

    def _recover_chat(self):
        """Восстановить `_chat` из `active_chat`, если backend-объект потерян.

        `_chat` намеренно исключён из pickle (см. `__getstate__`), поэтому после
        перезапуска процесса Reflex (в dev-режиме — на каждое изменение кода) он
        становится None, хотя браузер (через LocalStorage `active_chat`) всё ещё
        помнит, какой чат открыт. Без явного восстановления любое место, которое
        делает `self._chat or Chat.create()`, молча заводит новый чат вместо
        продолжения существующего — ровно то, что выглядит как «чат раздвоился».
        Вызывай это перед любым `Chat.create()` по признаку `_chat is None`.
        """
        if self._chat is None and self.active_chat:
            try:
                self._chat = _chats().get(self.active_chat)
            except (ValueError, sqlite3.Error):
                self._chat = None
        return self._chat

    def _restore_backend_session(self):
        """Восстановить runtime-объекты после reload процесса Reflex.

        `_chat` и `_book` намеренно исключены из pickle, поэтому публичные ID могут
        быть заполнены при пустых backend vars. Любой долгий background event должен
        уметь поднять их заново перед работой.
        """
        self._recover_chat()
        if self._book is None:
            book_id = self._chat.book_id if self._chat is not None else self.document_id
            if book_id:
                self._book = Book(book_id)
        if self._chat is None:
            self._chat = Chat.create(self._book.book_id if self._book is not None else '')

    @staticmethod
    def _detached_chat(chat):
        """Скопировать Chat из Reflex StateProxy для работы вне mutable context."""
        return Chat(
            chat_id=str(chat.chat_id), book_id=str(chat.book_id),
            pending_book_id=str(chat.pending_book_id), title=str(chat.title),
            messages=[dict(message) for message in chat.messages],
        )

    def _catalog(self):
        try:
            catalog = get_repository().catalog()
            self.database_error = ""
            return catalog
        except (ValueError, Neo4jError, DriverError) as error:
            self.database_error = f"Библиотека недоступна: {error}"
            return []

    def _list_chats(self):
        try:
            chats = _chats().list()
        except sqlite3.Error as error:
            self.error = f"Хранилище чатов недоступно: {error}"
            return list(self.chats)
        titles = {book["id"]: book["title"] for book in self._catalog()}
        for chat in chats:
            book_id = chat.get("document_id") or ""
            chat["book"] = titles.get(book_id) or chat["book"]
        return chats

    def _sync_structure(self):
        self.can_undo_structure = self._book.can_undo_structure if self._book else False
        self.undo_structure_label = self._book.undo_structure_label if self._book else ""
        self.sections = self._book.outline() if self._book else []
        self.chapters = self._book.chapter_outline() if self._book else []
        self.structure_status = self._book.structure_status if self._book else "needs_review"
        self.structure_issues = "\n".join(self._book.issues[-8:]) if self._book else ""
        self.review_issues = [str(issue) for issue in self._book.issues] if self._book else []
        self.review_targets = self._book.review_items if self._book else []
        self.scene_items = []
        self.epigraph_items = []
        self.selectable_elements = []
        self.selectable_total = 0
        self.show_element_numbers = False
        self.selection_active = False
        self.selected_blocks = []
        self.scene_status = "Сцены ещё не построены"
        self.scene_issues = ""
        self.scene_has_manual = False
        self.scenes_ready = False
        self.entity_status = "Персонажи и места ещё не найдены"
        self.entity_items = []
        self.book_entity_status = "Персонажи и места книги ещё не собраны"
        self.book_entity_items = []
        if not self.sections:
            self.excerpt = "В сохранённой структуре нет разделов."
            return
        outline_indexes = [item["index"] for item in self.chapters]
        if not outline_indexes:
            outline_indexes = [item["index"] for item in self.sections]
        if self.selected not in outline_indexes:
            self.selected = outline_indexes[0]
        outline_position = outline_indexes.index(self.selected)
        self.can_previous_section = outline_position > 0
        self.can_next_section = outline_position < len(outline_indexes) - 1
        section = self._book.section(self.selected)
        self.title = section.get("title")
        self.selected_chapter_title = self.title or "выбранная глава"
        selectable = self._book.selectable_text(self.selected)
        self.selectable_total = len(selectable)
        last_page_start = max(0, ((len(selectable) - 1) // ELEMENT_PAGE_SIZE) * ELEMENT_PAGE_SIZE)
        self.element_page_start = min(max(0, self.element_page_start), last_page_start)
        page_end = min(self.element_page_start + ELEMENT_PAGE_SIZE, len(selectable))
        self.selectable_elements = selectable[self.element_page_start:page_end]
        self.can_previous_elements = self.element_page_start > 0
        self.can_next_elements = page_end < len(selectable)
        self.element_page_label = (
            f"{self.element_page_start + 1}–{page_end} из {len(selectable)}"
            if selectable else ""
        )
        self.epigraph_items = self._book.epigraphs(self.selected)
        self.excerpt = "" if selectable else self._book.text(
            self.selected, self.show_element_numbers,
        )
        layer = self._book.scene_layer(self.selected)
        self.scene_has_manual = layer.get("human_edited", False)
        self.scene_status = {
            "not_started": "Сцены ещё не построены",
            "ready": "Сцены построены",
            "stale": "Границы раздела изменены — сцены нужно пересчитать",
            "partial": "Обработана только часть раздела",
            "failed": "Не удалось построить сцены",
        }.get(layer["status"], layer["status"])
        self.scene_issues = "\n".join(layer.get("issues", []))
        self.scene_items = self._book.scene_previews(self.selected)
        self.scenes_ready = layer.get("status") == "ready" and bool(self.scene_items)
        if self.scenes_ready:
            entities = self._book.chapter_entities(self.selected)
            self.entity_items = [
                {
                    "id": str(entity["id"]),
                    "name": str(entity.get("canonical_name") or "Без имени"),
                    "details": (
                        f"{entity_type_label(entity.get('entity_type'))} · "
                        f"упоминаний: {len(entity.get('mention_ids', []))}"
                    ),
                }
                for entity in entities
            ]
            if entities:
                self.entity_status = f"В главе найдено: {len(entities)}"
        book_entities = self._book.book_entities()
        self.book_entity_items = [
            {
                "id": str(entity["id"]),
                "name": str(entity.get("canonical_name") or "Без имени"),
                "details": (
                    f"{entity_type_label(entity.get('entity_type'))} · "
                    f"глав: {len(entity.get('chapter_ids', []))}"
                ),
            }
            for entity in book_entities
        ]
        if book_entities:
            self.book_entity_status = f"В книге: {len(book_entities)}"
        self._sync_preparation_details()
    # ═══ Жизненный цикл ═════════════════════════════════════════

    def _open(self, book: Book | None, chat: Chat):
        self._book = book
        self._chat = chat
        self.active_chat = chat.chat_id
        self.document_id = book.book_id if book else ""
        self.selected = "0"
        self.tab = "chat"
        self.error = ""
        self.selection_error = ""
        self.selected_blocks = []
        self.selection_active = False
        self.choosing_book = False
        self.findings_searches = []
        self.findings_selected_search = ""
        self.findings_query = ""
        self.findings_items = []
        self.findings_next_offset = ""
        self.findings_status = ""
        self._sync()

    @rx.event
    def load(self):
        self.selection_error = ""
        self.selected_blocks = []
        self.selection_active = False
        if self._chat is not None:
            return
        self._book = None
        self._chat = None
        self.document_id = ""
        self.documents = self._catalog()
        self.chats = self._list_chats()
        if self.chats:
            chat_id = self.active_chat if any(c["id"] == self.active_chat for c in self.chats) else self.chats[0]["id"]
            self.open_chat(chat_id)
        else:
            self.new_chat()

    @rx.event
    def open_document(self, document_id: str):
        if not self._can_proceed():
            return
        try:
            if (book_dir(document_id) / "upload.json").exists():
                process_upload(document_id)
            book = Book(document_id)
            chat = self._recover_chat() or Chat.create()
            if chat.pending_book_id and chat.pending_book_id != document_id:
                raise ValueError("К этому чату уже добавлена другая книга для обработки.")
            repository = _chats()
            repository.ensure(chat)
            # Повторное открытие той же книги не должно снова писать в чат о подключении.
            newly_attached = chat.book_id != document_id
            chat.attach(document_id)
            if newly_attached:
                chat.add("assistant", f"Книга «{book.title}» подключена. Опишите, какие "
                                      "фрагменты найти.")
                repository.append_message(chat, chat.messages[-1])
            self.pending_upload = ""
            self.upload_status = ""
            self._open(book, chat)
        except (ValueError, OSError, KeyError, sqlite3.Error, Neo4jError, DriverError) as error:
            self.error = str(error)

    @rx.event
    def open_chat(self, chat_id: str):
        if not self._can_proceed():
            return
        try:
            chat = _chats().get(chat_id)
            if chat.book_id and (book_dir(chat.book_id) / "upload.json").exists():
                process_upload(chat.book_id)
            book = Book(chat.book_id) if chat.book_id else None
            self._open(book, chat)
            self.pending_upload = chat.pending_book_id
            self.upload_status = ""
            if self.pending_upload:
                info = upload_info(self.pending_upload)
                if info["status"] == "ready":
                    self.open_document(self.pending_upload)
                else:
                    self.upload_status = "Книга сохранена. Можно продолжить обработку."
        except (ValueError, OSError, KeyError, sqlite3.Error, Neo4jError, DriverError) as error:
            self.error = str(error)

    @rx.event
    def new_chat(self):
        if not self._can_proceed():
            return
        self._book = None
        self._chat = Chat.create()
        self.active_chat = ""
        self.document_id = ""
        self.book = ""
        self.chat_title = "Новый чат"
        self.user_prompt = ""
        self.llm_enabled = PhilologyAgent.llm_enabled()
        self.messages = []
        self.sections = []
        self.excerpt = ""
        self.title = ""
        self.selected = "0"
        self.tab = "chat"
        self.error = ""
        self.upload_status = ""
        self.pending_upload = ""
        self.choosing_book = False

    @rx.event
    def choose_book(self):
        if not self._can_proceed(forbid_document=True) or self.pending_upload:
            return
        self.documents = self._catalog()
        self.choosing_book = True

    @rx.event
    def request_delete_chat(self, chat_id: str):
        if self.busy or self.processing_book:
            return
        chat = next((item for item in self.chats if item["id"] == chat_id), None)
        if chat is None:
            return
        self.delete_chat_id = chat_id
        self.delete_chat_title = chat["title"]
        self.delete_dialog_open = True

    @rx.event
    def set_delete_dialog_open(self, value: bool):
        self.delete_dialog_open = value
        if not value:
            self.delete_chat_id = ""
            self.delete_chat_title = ""

    @rx.event
    def delete_chat(self):
        chat_id = self.delete_chat_id
        if not self.delete_dialog_open or not chat_id or self.busy or self.processing_book:
            return
        try:
            _chats().delete(chat_id)
            self.chats = self._list_chats()
            self.error = ""
            self.set_delete_dialog_open(False)
            if self.active_chat == chat_id:
                self.new_chat()
                self.user_prompt = ""
                if self.chats:
                    self.open_chat(self.chats[0]["id"])
        except (ValueError, OSError, KeyError, sqlite3.Error, Neo4jError, DriverError) as error:
            self.set_delete_dialog_open(False)
            self.error = str(error)

    # ═══ Загрузка книги ═════════════════════════════════════════

    async def _read_one_file(self, files: list[rx.UploadFile]) -> tuple[str, bytes]:
        try:
            if len(files) != 1:
                raise ValueError("Выбери один файл книги.")
            name = files[0].filename or "book"
            data = await files[0].read(MAX_BOOK_BYTES + 1)
            return name, data
        finally:
            for file in files:
                await file.close()

    @rx.event
    async def upload_book(self, files: list[rx.UploadFile]):
        if not self._can_proceed(forbid_document=True) or self.pending_upload:
            for file in files:
                await file.close()
            return
        self.processing_book = True
        self.error = ""
        self.upload_status = "Сохраняю книгу…"
        yield
        try:
            name, data = await self._read_one_file(files)
            self.pending_upload = await asyncio.to_thread(save_upload, name, data)
            if self._recover_chat() is None:
                self._chat = Chat.create()
            repository = _chats()
            repository.ensure(self._chat)
            self._chat.pending_book_id = self.pending_upload
            repository.update(self._chat)
            self.active_chat = self._chat.chat_id
            self.chats = self._list_chats()
            self.choosing_book = False
            self.upload_status = "Оригинал сохранён. Начинаю обработку…"
        except ValueError as error:
            self.error = str(error)
            self.upload_status = ""
        except Exception as error:
            self.error = f"Не удалось сохранить файл ({type(error).__name__})."
            self.upload_status = ""
        finally:
            self.processing_book = False
        if self.pending_upload and not self.error:
            yield State.process_book

    @rx.event(background=True)
    async def process_book(self):
        async with self:
            if self.processing_book or not self.pending_upload or self._chat is None:
                return
            self.processing_book = True
            chat = self._chat
            self.error = ""
            document_id = self.pending_upload
            self.upload_status = (
                "Оригинал сохранён. Извлекаю текст и определяю структуру…"
            )
        try:
            await asyncio.to_thread(process_upload, document_id)
            book = await asyncio.to_thread(Book, document_id)
            async with self:
                repository = _chats()
                repository.ensure(chat)
                chat.attach(document_id)
                chat.add("assistant", f"Книга «{book.title}» загружена. Чтобы искать по смыслу "
                                      "и связям, подготовьте её во вкладке «Подготовка».")
                repository.append_message(chat, chat.messages[-1])
                self._open(book, chat)
                self.pending_upload = ""
                self.upload_status = ""
                self.documents = self._catalog()
        except Exception as error:
            async with self:
                detail = str(error).strip()
                self.error = (
                    f"Обработка прервалась: {detail or type(error).__name__}. "
                    f"Оригинал сохранён; можно повторить обработку."
                )
                self.upload_status = "Оригинал сохранён"
        finally:
            async with self:
                self.processing_book = False

    # ═══ Редактирование структуры ═══════════════════════════════

    @rx.event
    def set_replace_human_scenes(self, value: bool):
        self.replace_human_scenes = value

    @rx.event
    def set_preparation_scope(self, value: str):
        if value not in {'chapter', 'book'}:
            raise ValueError('Область подготовки: chapter или book')
        self.preparation_scope = value
        self._sync_preparation_details()

    @rx.event
    def set_preparation_embeddings(self, value: bool):
        self.preparation_embeddings = value
        self._sync_preparation_details()

    def _sync_preparation_details(self):
        if not self._book or not self.chapters:
            self.preparation_details = ""
            self.preparation_rows = []
            self.preparation_summary = ""
            return
        indexes = ([self.selected] if self.preparation_scope == 'chapter'
                   else [item['index'] for item in self.chapters])
        try:
            status = self._book.preparation_status(indexes)
        except (ValueError, Neo4jError, DriverError) as error:
            self.preparation_details = f"Состояние подготовки недоступно: {error}"
            self.preparation_rows = []
            return
        self.preparation_details = technical_details(status)
        titles = self._book.chapter_titles()
        self.preparation_rows = chapter_table(
            status, titles, with_embeddings=self.preparation_embeddings,
            failures=self.preparation_failures,
        )
        stage_fields = ('scenes_state', 'chunks_state', 'mentions_state',
                        'chapter_entities_state')
        done = sum(all(row[field] == 'done' for field in stage_fields)
                   and not row['error'] for row in self.preparation_rows)
        self.preparation_summary = (
            f"Подготовлено глав: {done} из {len(self.preparation_rows)}"
            if self.preparation_rows else "")

    def _apply_preparation_rows(self, rows=None):
        # Оставлено как совместимый hook для живого progress. Таблица строится
        # из сохранённого per-chapter status и ошибок последнего запуска.
        return

    @rx.event(background=True)
    async def build_selected_scenes(self):
        async with self:
            if not self._can_proceed(need_session=True):
                return
            self.busy = True
            self.error = ""
            self.scene_status = "Строю сцены…"
            book = self._book
            selected = self.selected
            replace_human = self.replace_human_scenes
            # Тот же выбор, что и у «Подготовить»: без галочки сцены строятся без
            # эмбеддингов, их можно добавить позже без повторного вызова LLM.
            with_embeddings = self.preparation_embeddings
        try:
            embedder = configured_embedder() if with_embeddings else None
            await asyncio.to_thread(_with_llm, lambda client, config: book.build_scenes(
                selected, client, config=config,
                replace_human=replace_human, embedder=embedder,
            ))
        except Exception as error:
            async with self:
                self.error = describe_error(error)
        finally:
            async with self:
                self.busy = False
                if self._book is book:
                    self._sync_structure()
                    self._sync_header()

    @rx.event(background=True)
    async def prepare_graph_scope(self):
        """Сцены → фрагменты/эмбеддинги → персонажи и места для главы или книги."""
        async with self:
            if not self._can_proceed(need_session=True):
                return
            self.busy = True
            self.preparation_active = True
            self.preparation_cancelling = False
            cancel_token = uuid4().hex
            cancel_event = Event()
            with _PREPARATION_CANCELLATIONS_LOCK:
                _PREPARATION_CANCELLATIONS[cancel_token] = cancel_event
            self.preparation_cancel_token = cancel_token
            self.error = ""
            self.preparation_error = ""
            self.preparation_error_detail = ""
            self.preparation_failed = ""
            self.preparation_running = ""
            scope = self.preparation_scope
            with_embeddings = self.preparation_embeddings
            book = self._book
            indexes = ([self.selected] if scope == 'chapter'
                       else [item['index'] for item in self.chapters])
            chapter_ids = [book.section(index)['id'] for index in indexes]
            if self.preparation_failed_chapters:
                failed = set(self.preparation_failed_chapters)
                retry_ids = [chapter_id for chapter_id in chapter_ids
                             if chapter_id in failed]
                if retry_ids:
                    chapter_ids = retry_ids
            titles = book.chapter_titles()
            self.preparation_failed_chapters = []
            self.preparation_failures = []
            self.preparation_status = (
                f"Готовлю {'главу «' + self.selected_chapter_title + '»' if scope == 'chapter' else 'всю книгу'}…"
            )
            self.preparation_progress = ""
        loop = asyncio.get_running_loop()

        async def show_progress(key, position, total, chapter_id):
            async with self:
                if not self.busy:
                    return
                self.preparation_running = progress_row(key)
                self.preparation_progress = progress_text(
                    key, position, total, titles.get(chapter_id) if chapter_id else None)
                if chapter_id:
                    rows = [dict(row) for row in self.preparation_rows]
                    stage_field = {
                        'scenes': 'scenes_state',
                        'mentions': 'mentions_state',
                        'chapter_entities': 'chapter_entities_state',
                    }.get(key)
                    for row in rows:
                        if row.get('chapter_id') != chapter_id:
                            continue
                        if key in {'mentions', 'chapter_entities'}:
                            row['scenes_state'] = 'done'
                            row['chunks_state'] = 'done'
                        if key == 'chapter_entities':
                            row['mentions_state'] = 'done'
                        if stage_field:
                            row[stage_field] = 'running'
                    self.preparation_rows = rows

        def progress(key, position, total, chapter_id):
            # Предварительная техническая проверка не является этапом книги и
            # не должна мелькать в пользовательском прогрессе.
            if key == 'embeddings_check':
                return
            asyncio.run_coroutine_threadsafe(
                show_progress(key, position, total, chapter_id), loop)

        try:
            embedder = configured_embedder() if with_embeddings else None
            result = await asyncio.to_thread(_with_llm, lambda client, config: (
                BookPreparationService(
                    book.book_id, book.service.repository, client, config,
                    embedder=embedder,
                ).prepare(
                    chapter_ids, with_embeddings=with_embeddings,
                    full_rebuild=scope == 'book', progress=progress,
                    continue_on_error=True,
                    cancelled=cancel_event.is_set,
                )
            ))
            index = result['index']
            graph = result['book_entities']['graph']
            skipped = result.get('skipped') or []
            failures = result.get('failures') or []
            global_errors = result.get('global_errors') or []
            async with self:
                self.preparation_failed_chapters = result.get('failed_chapter_ids') or []
                self.preparation_failures = [dict(
                    chapter_id=error.chapter_id or '', key=error.key or '',
                    message=readable_error(error, titles), detail=error.detail,
                ) for error in failures]
                done = len(result.get('completed_chapter_ids') or [])
                failed_count = len(self.preparation_failed_chapters)
                self.preparation_status = (
                    f"{done} из {done + failed_count} готово"
                    + (f", {failed_count} не удалось." if failed_count else ".")
                    + f" Фрагментов текста {index['chunks']}"
                    + (f", с эмбеддингами {index['chunks_with_embeddings']}" if with_embeddings else "")
                    + f", связей персонажей со сценами {graph['presence_edges']}."
                    + (" Пропущены без основного текста: "
                       + ", ".join(f"«{titles.get(chapter_id, chapter_id)}»" for chapter_id in skipped)
                       + "." if skipped else "")
                )
                if failures:
                    self.preparation_error = (
                        f"Не удалось подготовить глав: {failed_count}. "
                        "Причины доступны в «Подробностях»; «Повторить» запустит только их."
                    )
                    self.preparation_error_detail = '\n'.join(
                        readable_error(error, titles) for error in failures)
                if global_errors:
                    self.preparation_error = readable_error(global_errors[0], titles)
                    self.preparation_error_detail = '\n'.join(
                        describe_error(error) for error in global_errors)
        except PreparationCancelled:
            async with self:
                self.preparation_failed_chapters = list(chapter_ids)
                self.preparation_status = (
                    "Подготовка отменена. Готовые результаты сохранены; "
                    "повторный запуск продолжит с них."
                )
                self.preparation_error = ""
                self.preparation_error_detail = ""
        except Exception as error:
            logger.warning("Book preparation stopped: %s", describe_error(error))
            async with self:
                self.preparation_failed = progress_row(getattr(error, 'key', None) or self.preparation_running)
                self.preparation_error = readable_error(error, titles)
                self.preparation_error_detail = describe_error(error)
                self.preparation_status = "Подготовка остановлена. Готовые этапы сохранены — «Повторить» продолжит с места остановки."
        finally:
            with _PREPARATION_CANCELLATIONS_LOCK:
                _PREPARATION_CANCELLATIONS.pop(cancel_token, None)
            async with self:
                self.busy = False
                self.preparation_active = False
                self.preparation_cancelling = False
                self.preparation_cancel_token = ""
                self.preparation_running = ""
                self.preparation_progress = ""
                if self._book is book:
                    book.reload()
                    self._sync_structure()
                    self._sync_header()

    @rx.event
    def cancel_preparation(self):
        if not self.preparation_active or not self.preparation_cancel_token:
            return
        with _PREPARATION_CANCELLATIONS_LOCK:
            cancel_event = _PREPARATION_CANCELLATIONS.get(
                self.preparation_cancel_token)
        if cancel_event is not None:
            cancel_event.set()
            self.preparation_cancelling = True
            self.preparation_status = "Останавливаю после текущего запроса…"

    @rx.event(background=True)
    async def analyze_selected_entities(self):
        async with self:
            if not self._can_proceed(need_session=True):
                return
            if not self.scenes_ready:
                self.error = "Сначала постройте актуальные сцены выбранной главы"
                return
            self.busy = True
            self.error = ""
            self.entity_status = "Ищу персонажей и места в главе…"
            book = self._book
            selected = self.selected
        try:
            result = await asyncio.to_thread(_with_llm, lambda client, config: (
                book.analyze_chapter_entities(selected, client, config=config)
            ))
            async with self:
                self.entity_status = (
                    f"Готово: найдено {len(result['entities'])} "
                    f"(до объединения повторов — {len(result['provisional_entities'])})"
                )
        except Exception as error:
            async with self:
                self.error = describe_error(error)
                self.entity_status = "Не удалось найти персонажей и места"
        finally:
            async with self:
                self.busy = False
                if self._book is book:
                    self._sync_structure()
                    self._sync_header()

    @rx.event(background=True)
    async def resolve_book_entities(self):
        async with self:
            if not self._can_proceed(need_session=True):
                return
            self.busy = True
            self.error = ""
            self.book_entity_status = "Собираю персонажей и места по всей книге…"
            book = self._book
        try:
            result = await asyncio.to_thread(_with_llm, lambda client, config: (
                book.resolve_book_entities(client, config=config)
            ))
            async with self:
                resolved = sum(
                    row.get("entity_id") is not None for row in result["decisions"]
                )
                uncertain = sum(
                    row.get("decision") == "uncertain" for row in result["decisions"]
                )
                self.book_entity_status = (
                    f"Готово: в книге {len(result['entities'])}, "
                    f"сопоставлено {resolved}, под вопросом {uncertain}; "
                    f"связей со сценами {result['graph']['presence_edges']}"
                )
        except Exception as error:
            async with self:
                self.error = describe_error(error)
                self.book_entity_status = "Не удалось собрать персонажей и места книги"
        finally:
            async with self:
                self.busy = False
                if self._book is book:
                    self._sync_structure()
                    self._sync_header()

    @rx.event(background=True)
    async def reconcile_book_entities(self):
        """Повторно сверить уже разрешённые сущности книги: находит пропущенные
        merge (см. entities.book_resolution.BookEntityResolver.reconcile),
        например diminutive, для которого не хватило контекста при первом
        проходе. Ничего не делает молча — статус всегда показывает результат."""
        async with self:
            if not self._can_proceed(need_session=True):
                return
            self.busy = True
            self.error = ""
            self.book_entity_status = "Ищу повторы среди персонажей и мест…"
            book = self._book
        try:
            result = await asyncio.to_thread(_with_llm, lambda client, config: (
                book.reconcile_book_entities(client, config=config)
            ))
            async with self:
                merges = len(result["merges"])
                self.book_entity_status = (
                    "Повторов не найдено" if not merges else
                    f"Объединено повторов: {merges}; "
                    f"осталось {len(result['entities']) - sum(len(m['loser_ids']) for m in result['merges'])}"
                )
        except Exception as error:
            async with self:
                self.error = describe_error(error)
                self.book_entity_status = "Не удалось проверить повторы"
        finally:
            async with self:
                self.busy = False
                if self._book is book:
                    self._sync_structure()
                    self._sync_header()

    # ═══ Находки ════════════════════════════════════════════════

    def _findings(self):
        return Findings(self._book.book_id, self._chat.chat_id, book_title=self._book.title)

    def _load_finding_items(self):
        """Первая страница выбранного поиска с текущим фильтром и счётчики статусов."""
        findings = self._findings()
        status = self.findings_filter or None
        items, next_offset, query = findings.candidates(self.findings_selected_search, status=status)
        counts = findings.counts(self.findings_selected_search)
        self.findings_items = items
        self.findings_next_offset = str(next_offset) if next_offset is not None else ""
        self.findings_query = query
        total = sum(counts.values())
        self.findings_counts = [dict(status="", label="Все", count=str(total))] + [
            dict(status=key, label=STATUS_LABELS[key], count=str(counts.get(key, 0)))
            for key in STATUS_ORDER
        ]
        self.findings_status = "" if items else (
            "Нет фрагментов с таким статусом." if status else "У этого поиска пока нет кандидатов."
        )

    def _load_findings(self):
        try:
            self.findings_searches = self._findings().searches()
            self.findings_status = "" if self.findings_searches else "Поисков в этом чате пока нет."
        except (ValueError, OSError, sqlite3.Error, Neo4jError, DriverError) as error:
            self.findings_status = str(error)
            self.findings_searches = []

    @rx.event
    def refresh_findings(self):
        if not self._can_proceed(need_session=True):
            return
        self._load_findings()

    @rx.event
    def open_finding_search(self, search_id: str):
        if not self._can_proceed(need_session=True):
            return
        self.findings_selected_search = search_id
        self.findings_filter = ""
        self.findings_items = []
        self.findings_next_offset = ""
        try:
            self._load_finding_items()
        except (ValueError, OSError, sqlite3.Error, Neo4jError, DriverError) as error:
            self.findings_status = str(error)

    @rx.event
    def set_findings_filter(self, status: str):
        if not self._can_proceed(need_session=True) or not self.findings_selected_search:
            return
        self.findings_filter = status
        try:
            self._load_finding_items()
        except (ValueError, OSError, sqlite3.Error, Neo4jError, DriverError) as error:
            self.findings_status = str(error)

    @rx.event
    def review_finding(self, candidate_id: str, status: str):
        """Решение исследователя по фрагменту (SearchService.review_candidate)."""
        if not self._can_proceed(need_session=True) or not self.findings_selected_search:
            return
        item = next((row for row in self.findings_items if row["id"] == candidate_id), None)
        if item is None or item["status"] == status:
            return
        try:
            self._findings().review(self.findings_selected_search, candidate_id, status,
                                    item.get("reason", ""))
            self._load_finding_items()
        except (ValueError, OSError, sqlite3.Error, Neo4jError, DriverError) as error:
            self.findings_status = str(error)

    @rx.event
    def show_finding_in_text(self, section_id: str):
        """Открыть главу фрагмента в «Разметке» (без точной подсветки фрагмента)."""
        if not self._can_proceed(need_session=True):
            return
        index = self._book.section_index(section_id) if section_id else None
        if index is None:
            self.findings_status = "Раздел этого фрагмента больше не найден в разметке."
            return
        self.selected = index
        self.element_page_start = 0
        self._sync_structure()
        self.tab = "structure"
        return rx.call_script(SCROLL_BOOK_TO_TOP_JS)

    @rx.event
    def open_latest_findings(self):
        """Из ответа в чате — сразу к самому свежему поиску этого чата."""
        if not self._can_proceed(need_session=True):
            return
        self.tab = "findings"
        self._load_findings()
        if self.findings_searches:
            return State.open_finding_search(self.findings_searches[0]["id"])

    @rx.event
    def load_more_findings(self):
        if not self._can_proceed(need_session=True) or not self.findings_next_offset:
            return
        try:
            items, next_offset, _ = self._findings().candidates(
                self.findings_selected_search, offset=int(self.findings_next_offset),
                status=self.findings_filter or None,
            )
            self.findings_items = self.findings_items + items
            self.findings_next_offset = str(next_offset) if next_offset is not None else ""
        except (ValueError, OSError, sqlite3.Error, Neo4jError, DriverError) as error:
            self.findings_status = str(error)

    @rx.event
    def set_findings_include_all(self, value: bool):
        self.findings_include_all = value

    @rx.event
    def download_findings_markdown(self):
        if not self._can_proceed(need_session=True) or not self.findings_selected_search:
            return
        try:
            content = self._findings().export_markdown(
                self.findings_selected_search, include_all=self.findings_include_all,
            )
            return rx.download(data=content, filename=export_filename(self.findings_query, "md"))
        except (ValueError, OSError, sqlite3.Error, Neo4jError, DriverError) as error:
            self.findings_status = str(error)

    @rx.event
    def toggle_findings_list(self):
        self.findings_list_hidden = not self.findings_list_hidden

    @rx.event
    def download_findings_csv(self):
        if not self._can_proceed(need_session=True) or not self.findings_selected_search:
            return
        try:
            content = self._findings().export_csv(
                self.findings_selected_search, include_all=self.findings_include_all,
            )
            return rx.download(data=content, filename=export_filename(self.findings_query, "csv"),
                               mime_type="text/csv")
        except (ValueError, OSError, sqlite3.Error, Neo4jError, DriverError) as error:
            self.findings_status = str(error)

    @rx.event
    def download_findings_json(self):
        if not self._can_proceed(need_session=True) or not self.findings_selected_search:
            return
        try:
            content = self._findings().export_json(
                self.findings_selected_search, include_all=self.findings_include_all,
            )
            return rx.download(data=content, filename=export_filename(self.findings_query, "json"))
        except (ValueError, OSError, sqlite3.Error, Neo4jError, DriverError) as error:
            self.findings_status = str(error)

    @rx.event
    def open_scene_editor(self, scene_id: str):
        if not self._can_proceed(need_session=True):
            return
        scene = next(s for s in self.scene_items if s["id"] == scene_id)
        self.scene_id = scene_id
        self.scene_title, self.scene_summary = scene["title"], scene["summary"]
        self.scene_range = f"Длина сцены: {int(scene['end']) - int(scene['start'])} символов."
        self.scene_editor_open = True

    @rx.event
    def set_scene_editor_open(self, value: bool):
        self.scene_editor_open = value

    @rx.event
    def apply_scene(self, values: dict):
        if not self._can_proceed(need_session=True):
            return
        with self._handle_errors():
            operation = {
                "Разделить сцену": "split", "Объединить с правой сценой": "merge_next",
                "Перенести правую границу": "move_boundary", "edit": "edit",
            }.get(values.get("operation", "edit"))
            changes = {}
            if operation == "edit":
                changes = dict(title=values.get("title", ""), summary=values.get("summary", ""))
            elif operation in {"split", "move_boundary"}:
                changes = dict(boundary=int(values.get("boundary", "")))
            self._book.edit_scene(self.selected, self.scene_id, operation, **changes)
            self.scene_editor_open = False
            self._sync_structure()
            self._sync_header()

    @rx.event
    def select_section(self, index: str):
        if not self._can_proceed(need_session=True):
            return
        self.selected = index
        self.scene_editor_open = False
        self.replace_human_scenes = False
        self.error = ""
        self.selection_error = ""
        self.selection_notice = ""
        self.section_editor_open = False
        self.element_page_start = 0
        self._sync_structure()
        return rx.call_script(SCROLL_BOOK_TO_TOP_JS)

    @rx.event
    def select_review_item(self, index: str, position: str, end_position: str,
                           block_type: str):
        if not self._can_proceed(need_session=True) or not index:
            return
        try:
            start = int(position)
            end = int(end_position)
        except (TypeError, ValueError):
            return
        if start < 0 or end <= start:
            return
        self.selected = index
        self.scene_editor_open = False
        self.replace_human_scenes = False
        self.error = ""
        self.selection_error = ""
        self.selection_notice = ""
        rows = self._book.selectable_text(index)
        target_row = next(
            (
                row_index for row_index, row in enumerate(rows)
                if int(row["position"]) < end and start < int(row["end_position"])
            ),
            0,
        )
        self.element_page_start = (target_row // ELEMENT_PAGE_SIZE) * ELEMENT_PAGE_SIZE
        self._sync_structure()
        return rx.call_script(_highlight_review_block_js(start, end, block_type))

    @rx.event
    def select_adjacent_section(self, offset: int):
        if not self._can_proceed(need_session=True) or offset not in {-1, 1}:
            return
        indexes = [item["index"] for item in self.chapters] or [
            item["index"] for item in self.sections
        ]
        if self.selected not in indexes:
            return
        target = indexes.index(self.selected) + offset
        if 0 <= target < len(indexes):
            self.selected = indexes[target]
            self.scene_editor_open = False
            self.replace_human_scenes = False
            self.error = ""
            self.element_page_start = 0
            self._sync_structure()
            return rx.call_script(SCROLL_BOOK_TO_TOP_JS)


    @rx.event
    def approve(self):
        if not self._can_proceed(need_session=True):
            return
        with self._handle_errors():
            self._book.approve()
            self._sync_header()
            self._sync_structure()

    @rx.event
    def undo_structure(self):
        if not self._can_proceed(need_session=True) or not self.can_undo_structure:
            return
        try:
            label = self.undo_structure_label
            self._book.undo_structure()
            self.selected_blocks = []
            self.selection_active = False
            self.section_editor_open = False
            self._sync_structure()
            self._sync_header()
            return self._notice_with_autohide(
                f"Отменено: {label.lower()}." if label else "Последнее изменение отменено."
            )
        except (ValueError, KeyError, TypeError) as error:
            self.selection_error = str(error)

    @rx.event
    def download(self):
        if not self._can_proceed(need_session=True):
            return
        try:
            return rx.download(
                data=self._book.export(),
                filename="structure.reviewed.json",
            )
        except (ValueError, OSError) as error:
            self.error = str(error)

    @rx.event
    def set_show_element_numbers(self, value: bool):
        self.show_element_numbers = value
        if self._book is not None and self.sections:
            self.excerpt = self._book.text(self.selected, value)

    @rx.event
    def change_element_page(self, offset: int):
        if offset not in {-1, 1} or not self._can_proceed(need_session=True):
            return
        target = self.element_page_start + offset * ELEMENT_PAGE_SIZE
        if target < 0 or target >= self.selectable_total:
            return
        self.element_page_start = target
        self.selected_blocks = []
        self.selection_active = False
        self._sync_structure()
        return rx.call_script(SCROLL_BOOK_TO_TOP_JS)

    @rx.event
    def capture_text_selection(self, selection: dict):
        if not isinstance(selection, dict):
            return

        self.selection_error = ""
        if not selection.get("valid"):
            self.selection_active = False
            if not selection.get("silent"):
                self.selection_error = selection.get("message", "Не удалось прочитать выделение.")
            return
        self.selection_start = int(selection["start"])
        self.selection_end = int(selection["end"])
        self.selection_left = f'{int(selection.get("x", 16))}px'
        self.selection_top = f'{int(selection.get("y", 16))}px'
        self.selection_active = True

    @rx.event
    def clear_selection_notice(self, token: int = -1):
        if token == self.selection_notice_token:
            self.selection_notice = ""

    @rx.event
    def clear_text_selection(self):
        self.selection_active = False

    @rx.event
    def set_text_context_menu_open(self, value: bool):
        self.text_context_menu_open = value

    @rx.event
    def open_section_editor(self):
        if not self._can_proceed(need_session=True):
            return
        section = self._book.section(self.selected)
        if self.selected_blocks:
            _, start, end = self._selected_block_bounds()
            self.section_editor_mode = "create"
            self.section_edit_title = ""
            self.section_edit_role = "section"
            self.section_edit_start = str(start)
            self.section_edit_end = str(end)
            self.section_edit_title_position = ""
            self.section_edit_level = str((section.get("level") or 1) + 1)
        else:
            self.section_editor_mode = "edit"
            self.section_edit_title = section.get("title") or ""
            self.section_edit_role = section.get("role") or "chapter"
            self.section_edit_start = str(section["start"])
            self.section_edit_end = "" if section.get("end") is None else str(section["end"])
            self.section_edit_title_position = (
                "" if section.get("title_position") is None
                else str(section["title_position"])
            )
            self.section_edit_level = str(section.get("level") or 1)
        self.section_edit_start_preview = self._book.element_preview(self.section_edit_start)
        self.section_edit_end_preview = self._book.element_preview(self.section_edit_end)
        self.selection_error = ""
        self.section_editor_open = True

    @rx.event
    def set_section_editor_open(self, value: bool):
        self.section_editor_open = value

    @rx.event
    def set_section_edit_start(self, value: str):
        self.section_edit_start = value
        self.section_edit_start_preview = self._book.element_preview(value) if self._book else ""

    @rx.event
    def set_section_edit_end(self, value: str):
        self.section_edit_end = value
        self.section_edit_end_preview = self._book.element_preview(value) if self._book else ""

    @rx.event
    def use_selected_section_bounds(self):
        try:
            _, start, end = self._selected_block_bounds()
            self.section_edit_start = str(start)
            self.section_edit_end = str(end)
            self.section_edit_start_preview = self._book.element_preview(start)
            self.section_edit_end_preview = self._book.element_preview(end)
        except (ValueError, KeyError, TypeError) as error:
            self.selection_error = str(error)

    @rx.event
    def apply_section_edit(self, values: dict):
        if not self._can_proceed(need_session=True):
            return
        try:
            if self.section_editor_mode == "create":
                self.selected = str(self._book.add_section(values))
                notice = "Новый раздел создан."
            else:
                self.selected = str(self._book.edit(self.selected, values))
                notice = "Раздел обновлён."
            self.section_editor_open = False
            self.selection_error = ""
            self._sync_structure()
            self._sync_header()
            return self._notice_with_autohide(notice)
        except (ValueError, KeyError, TypeError) as error:
            self.selection_error = str(error)

    @rx.event
    def toggle_block(self, position: str):
        self.selection_error = ""
        if position in self.selected_blocks:
            self.selected_blocks = [p for p in self.selected_blocks if p != position]
        else:
            self.selected_blocks = [*self.selected_blocks, position]

    @rx.event
    def clear_block_selection(self):
        self.selected_blocks = []
        self.selection_error = ""

    @rx.event
    def clear_selection_error(self):
        self.selection_error = ""

    def _selected_block_bounds(self) -> tuple[list[int], int, int]:
        positions = sorted(int(position) for position in self.selected_blocks)
        if not positions:
            raise ValueError("Сначала выберите хотя бы один блок.")
        rows = {int(row["position"]): row for row in self.selectable_elements}
        if any(position not in rows for position in positions):
            raise ValueError("Выбранные блоки устарели. Выберите их заново.")
        return (
            positions,
            positions[0],
            max(int(rows[position]["end_position"]) for position in positions),
        )

    @rx.event
    def apply_selected_blocks(self, operation: str):
        try:
            _, start, end = self._selected_block_bounds()
        except (ValueError, KeyError, TypeError) as error:
            self.selection_error = str(error)
            return
        return self._apply_text_selection(
            {"valid": True, "start": start, "end": end}, operation,
        )

    @rx.event
    def merge_selected_blocks(self):
        if not self._can_proceed(need_session=True):
            return
        try:
            positions, start, end = self._selected_block_bounds()
            if len(positions) < 2:
                raise ValueError("Выберите минимум два блока.")
            self._book.merge_elements(start, end)
            self.selected_blocks = []
            self._sync_structure()
            return self._notice_with_autohide(f"Объединено блоков: {len(positions)}.")
        except (ValueError, KeyError, TypeError) as error:
            self.selection_error = str(error)
            
    @rx.event
    def apply_captured_selection(self, operation: str):
        if self.selected_blocks:
            try:
                _, start, end = self._selected_block_bounds()
            except (ValueError, KeyError, TypeError) as error:
                self.selection_error = str(error)
                return
            return self._apply_text_selection(
                {"valid": True, "start": start, "end": end}, operation,
            )
        if self.selection_start < 0 or self.selection_end <= self.selection_start:
            self.selection_error = "Сначала выделите текст в выбранном разделе."
            self.selection_active = False
            return
        return self._apply_text_selection(
            {"valid": True, "start": self.selection_start, "end": self.selection_end},
            operation,
        )

    @rx.event
    def apply_text_selection(self, selection: dict, operation: str):
        return self._apply_text_selection(selection, operation)

    def _apply_text_selection(self, selection: dict, operation: str):
        if not self._can_proceed(need_session=True):
            return
        self.selection_error = ""
        self.selection_notice = ""
        if not isinstance(selection, dict) or not selection.get("valid"):
            self.selection_error = (
                selection.get("message", "Сначала выделите текст в выбранном разделе.")
                if isinstance(selection, dict) else
                "Сначала выделите текст в выбранном разделе."
            )
            return
        try:
            self._book.update_opening(
                self.selected, operation,
                int(selection["start"]), int(selection["end"]),
            )
            notice = {
                "epigraph": "Выделенный текст отмечен как эпиграф.",
                "attribution": "Источник эпиграфа сохранён.",
                "opening": "Выделенный текст отмечен как вступление.",
                "body": "Начало основного текста изменено.",
                "footnotes": "Выделенный текст отмечен как примечание.",
            }[operation]
            self.selection_active = False
            self._sync_structure()
            self._sync_header()
            return self._notice_with_autohide(notice)
        except (ValueError, KeyError, TypeError) as error:
            self.selection_error = str(error)

    @rx.event
    def merge_selection(self, selection: dict | None = None):
        if not self._can_proceed(need_session=True):
            return
        if selection is not None:
            if not isinstance(selection, dict) or not selection.get("valid"):
                self.selection_error = (
                    selection.get("message", "Сначала выделите абзацы.")
                    if isinstance(selection, dict) else "Сначала выделите абзацы."
                )
                return
            self.selection_start = int(selection["start"])
            self.selection_end = int(selection["end"])
        if self.selection_start < 0 or self.selection_end <= self.selection_start:
            self.selection_error = "Сначала выделите абзацы."
            return
        try:
            self._book.merge_elements(self.selection_start, self.selection_end)
            self.selection_active = False
            self._sync_structure()
            return self._notice_with_autohide("Абзацы объединены.")
        except (ValueError, KeyError) as error:
            self.selection_error = str(error)

    @rx.event
    def unmerge_selection(self, selection: dict | None = None):
        if not self._can_proceed(need_session=True):
            return
        if selection is not None:
            if not isinstance(selection, dict) or not selection.get("valid"):
                self.selection_error = (
                    selection.get("message", "Поставьте курсор на объединённый блок.")
                    if isinstance(selection, dict) else
                    "Поставьте курсор на объединённый блок."
                )
                return
            self.selection_start = int(selection["start"])
            self.selection_end = int(selection["end"])
        if self.selection_start < 0:
            self.selection_error = "Поставьте курсор на объединённый блок."
            return
        try:
            self._book.unmerge_at(self.selection_start)
            self.selection_active = False
            self._sync_structure()
            return self._notice_with_autohide("Блок разъединён.")
        except (ValueError, KeyError) as error:
            self.selection_error = str(error)

    # ═══ Сайдбар ════════════════════════════════════════════════

    @rx.event
    def toggle_sidebar(self):
        self.sidebar_hidden = not self.sidebar_hidden

    # ═══ Отправка сообщения ═════════════════════════════════════

    @rx.event(background=True)
    async def send(self, values: dict):
        prompt = values.get("user_prompt", "").strip()
        async with self:
            if self.busy or not prompt:
                return
            try:
                self._restore_backend_session()
            except (ValueError, OSError, KeyError, sqlite3.Error, Neo4jError, DriverError) as error:
                self.error = f"Не удалось восстановить выбранный чат: {error}"
                return
            self.busy = True
            self.agent_status = "Начинаю работу…"
            self.error = ""
            self.user_prompt = ""
            # Объекты, прочитанные непосредственно из StateProxy, становятся
            # immutable после выхода из `async with self`. AssistantService
            # добавляет сообщения, поэтому ему нужна независимая обычная копия.
            chat = self._detached_chat(self._chat)
            self.active_chat = chat.chat_id
            self.messages = _view_messages(
                list(chat.messages) + [{"role": "user", "content": prompt, "tools": ""}])
            self.chat_title = prompt[:60] if chat.title == "Новый чат" else chat.title

        try:
            loop = asyncio.get_running_loop()
            async def show_progress(message):
                async with self:
                    if self.busy:
                        self.agent_status = message
            def progress(message):
                asyncio.run_coroutine_threadsafe(show_progress(message), loop)
            await asyncio.to_thread(AssistantService(_chats()).send_message,
                                    chat, prompt, progress)
        except Exception as error:
            logger.exception("Frontend assistant turn failed")
            async with self:
                detail = str(error).strip() if isinstance(
                    error, (ValueError, OSError, KeyError, sqlite3.Error, Neo4jError, DriverError)
                ) else type(error).__name__
                self.error = f"Не удалось обработать сообщение: {detail or type(error).__name__}."

        async with self:
            self.busy = False
            self.agent_status = ""
            if self.active_chat == chat.chat_id:
                self._chat = chat
                self.messages = _view_messages(chat.messages)
                self.chat_title = chat.title
            self.chats = self._list_chats()
