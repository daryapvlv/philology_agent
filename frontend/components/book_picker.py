"""Прикрепление книги и статус обработки — общие для пустого и начатого чата."""
import reflex as rx
from ..state import State
from ..styles import MUTED, POPOVER_CONTENT, POPOVER_LIST, UPLOAD_ZONE, BOOK_CHOICE

def book_choice(book):
    return rx.button(
        rx.icon("book-open", size=22),
        rx.text(book["title"], white_space="normal", text_align="left"),
        rx.spacer(),
        rx.icon("arrow-right", size=18),
        on_click=State.open_document(book["id"]),
        disabled=State.busy | State.processing_book | (State.document_id != "") | (State.pending_upload != ""),
        **BOOK_CHOICE,
    )


# ═══════════════════════════════════════════════════════════════
#  ПОПАП ВЫБОРА КНИГИ
# ═══════════════════════════════════════════════════════════════

def _upload_zone():
    return rx.upload(
        rx.hstack(
            rx.icon("upload", size=18),
            rx.text("Загрузить новую книгу"),
            spacing="2", align="center",
        ),
        rx.text("EPUB или Markdown · до 50 МБ", size="1", color=MUTED),
        id="book-upload",
        accept={
            "application/epub+zip": [".epub"],
            "text/markdown": [".md", ".markdown"],
            "text/plain": [".md", ".markdown"],
        },
        multiple=False, max_files=1,
        disabled=State.busy | State.processing_book | (State.pending_upload != ""),
        on_drop=State.upload_book(rx.upload_files(upload_id="book-upload")),
        **UPLOAD_ZONE,
    )


def _library_list():
    return rx.fragment(
        rx.text("ИЛИ ВЫБРАТЬ ИЗ БИБЛИОТЕКИ", size="1", color=MUTED),
        rx.box(
            rx.vstack(
                rx.foreach(State.documents, book_choice),
                width="100%", spacing="2",
            ),
            **POPOVER_LIST,
        ),
        rx.cond(
            State.documents.length() == 0,
            rx.text("Здесь появятся загруженные книги.",
                    color=MUTED, size="2"),
        ),
    )


def _book_picker():
    return rx.popover.root(
        rx.popover.trigger(
            rx.button(
                rx.icon("plus", size=18),
                "Книга",
                variant="soft",
                disabled=State.busy | State.processing_book | (State.pending_upload != ""),
                on_click=State.choose_book,
            ),
        ),
        rx.popover.content(
            rx.vstack(
                rx.text("Добавить книгу в чат", weight="bold"),
                _upload_zone(),
                _library_list(),
                spacing="3", width="100%",
            ),
            **POPOVER_CONTENT,
        ),
        open=State.choosing_book,
        on_open_change=State.set_choosing_book,
    )


def _status_block():
    return rx.hstack(
        rx.cond(State.processing_book, rx.spinner(size="1")),
        rx.text(State.upload_status, size="2", color=MUTED),
        rx.spacer(),
        rx.cond(
            (State.pending_upload != "") & ~State.processing_book,
            rx.button(
                "Продолжить обработку",
                on_click=State.process_book,
                variant="soft", size="2",
            ),
        ),
        width="100%", align="center",
    )

