"""Боковая панель: список чатов, статус модели."""
import reflex as rx

from ..state import State
from ..styles import (
    MUTED, LINE, DOT_ON, DOT_OFF,
    SIDEBAR, SIDEBAR_HEADER, NEW_CHAT_BTN,
    SIDEBAR_SECTION_LABEL, SIDEBAR_LIST, SIDEBAR_FOOTER,
    STATUS_DOT, HOVER_BG,
    CHAT_LINK_BTN, CHAT_LINK_STACK, CHAT_LINK_TITLE, CHAT_LINK_BOOK,
    CHAT_ACTIONS, CHAT_ROW, CHAT_MENU, DELETE_DIALOG,
)


# ═══════════════════════════════════════════════════════════════
#  СТРОКА ЧАТА
# ═══════════════════════════════════════════════════════════════

def chat_link(item):
    is_active = State.active_chat == item["id"]

    return rx.hstack(
        rx.button(
            rx.vstack(
                rx.text(item["title"], **CHAT_LINK_TITLE),
                rx.text(item["book"], **CHAT_LINK_BOOK),
                **CHAT_LINK_STACK,
            ),
            on_click=State.open_chat(item["id"]),
            disabled=State.busy | State.processing_book,
            **CHAT_LINK_BTN,
        ),
        rx.menu.root(
            rx.menu.trigger(
                rx.icon_button(
                    "ellipsis", class_name="chat-actions",
                    aria_label="Действия с чатом «" + item["title"] + "»",
                    **CHAT_ACTIONS,
                ),
            ),
            rx.menu.content(
                rx.menu.item(
                    rx.icon("trash-2", size=16), "Удалить чат",
                    on_select=State.request_delete_chat(item["id"]),
                    disabled=State.busy | State.processing_book,
                    color_scheme="red",
                ),
                align="end", side="bottom", **CHAT_MENU,
            ),
        ),
        background=rx.cond(is_active, HOVER_BG, "transparent"),
        custom_attrs={"data-active": is_active},
        **CHAT_ROW,
    )


def _delete_dialog():
    """Одно окно подтверждения для выбранного чата."""
    return rx.alert_dialog.root(
        rx.alert_dialog.content(
            rx.alert_dialog.title("Удалить чат?", size="5"),
            rx.alert_dialog.description(
                "Переписка «", rx.text(State.delete_chat_title, as_="span", weight="medium"),
                "» будет удалена без возможности восстановления. Книга и её структура останутся в библиотеке.",
                size="2", color=MUTED, line_height="1.7", overflow_wrap="anywhere",
            ),
            rx.hstack(
                rx.alert_dialog.cancel(rx.button("Отмена", variant="soft", color_scheme="gray")),
                rx.button(
                    "Удалить чат", color_scheme="red", on_click=State.delete_chat,
                    disabled=State.busy | State.processing_book,
                ),
                justify="end", spacing="3", margin_top="24px",
            ),
            **DELETE_DIALOG,
        ),
        open=State.delete_dialog_open, on_open_change=State.set_delete_dialog_open,
    )


# ═══════════════════════════════════════════════════════════════
#  ЧАСТИ САЙДБАРА
# ═══════════════════════════════════════════════════════════════

def _header():
    """Логотип + кнопка сворачивания."""
    return rx.hstack(
        rx.icon("book-open", size=23),
        rx.spacer(),
        rx.icon_button(
            "panel-left-close",
            on_click=State.toggle_sidebar,
            variant="ghost",
            aria_label="Скрыть список чатов",
        ),
        **SIDEBAR_HEADER,
    )


def _new_chat():
    """Кнопка «Новый чат»."""
    return rx.button(
        rx.icon("square-pen", size=17),
        "Новый чат",
        on_click=State.new_chat,
        disabled=State.busy | State.processing_book,
        **NEW_CHAT_BTN,
    )


def _list():
    """Заголовок «ВАШИ ЧАТЫ» + скроллируемый список."""
    return rx.fragment(
        rx.text("ВАШИ ЧАТЫ", **SIDEBAR_SECTION_LABEL),
        rx.box(
            rx.vstack(
                rx.foreach(State.chats, chat_link),
                spacing="1", width="100%",
            ),
            **SIDEBAR_LIST,
        ),
    )


def _footer():
    """Статус модели + подпись."""
    return rx.vstack(
        rx.hstack(
            rx.box(
                background=rx.cond(State.llm_enabled, DOT_ON, DOT_OFF),
                **STATUS_DOT,
            ),
            rx.text(
                rx.cond(
                    State.llm_enabled,
                    "Модель подключена",
                    "Режим без модели",
                ),
                size="1",
            ),
            align="center",
        ),
        rx.text("Локальная библиотека диалогов", color=MUTED, size="1"),
        **SIDEBAR_FOOTER,
    )


# ═══════════════════════════════════════════════════════════════
#  САЙДБАР ЦЕЛИКОМ
# ═══════════════════════════════════════════════════════════════

def sidebar():
    return rx.vstack(
        _header(),
        _new_chat(),
        _list(),
        _footer(),
        _delete_dialog(),
        **SIDEBAR,
    )
