import reflex as rx

from ..state import State
from .book_picker import _book_picker, _status_block
from ..styles import (
    INK, MUTED, ACCENT,
    USER_BUBBLE, ASSISTANT_ROW,
    CHAT_STREAM, CHAT_SHELL,
    STRUCTURE_HINT, COMPOSER, SEND_BTN, DISCLAIMER,
    COMPOSER_TEXTAREA

)

# ═══════════════════════════════════════════════════════════════
#  СООБЩЕНИЯ ПО РОЛЯМ
# ═══════════════════════════════════════════════════════════════

def user_message(item):
    "Сообщение пользователя"
    return rx.box(
        rx.box(
            rx.text(item["content"], white_space="pre-wrap", line_height="1.7"),
            **USER_BUBBLE,
        ),
        width="100%", padding="16px 0",
        )

def _how_searched(item):
    """Свёрнутый блок «Как искала»: подвал ответа исследования и служебный след
    инструментов — видно по запросу, но не заслоняет сам ответ."""
    has_footer = item["footer"] != ""
    has_tools = item["tools"] != ""
    return rx.cond(
        has_footer | has_tools,
        rx.el.details(
            rx.el.summary(
                rx.cond(has_footer, "Как искала", "Подробности"),
                style={"cursor": "pointer", "color": MUTED, "font_size": "13px"},
            ),
            rx.vstack(
                rx.cond(has_footer, rx.box(rx.markdown(item["footer"]), color=MUTED,
                                           font_size="13px", width="100%")),
                rx.cond(has_tools, rx.text(item["tools"], size="1", color=MUTED,
                                           overflow_wrap="anywhere")),
                spacing="1", padding_top="6px", width="100%",
            ),
            width="100%",
        ),
    )


def assistant_message(item):
    return rx.box(
        rx.hstack(
            rx.box(
                rx.icon("sparkles", size=19, color=ACCENT),
                padding_top="5px", flex_shrink="0",
            ),
            rx.vstack(
                rx.box(
                    rx.markdown(item["body"]),
                    min_width="0", width="100%", line_height="1.8",
                ),
                _how_searched(item),
                rx.cond(
                    item["footer"] != "",
                    rx.button(rx.icon("table-2", size=14), "Открыть находки",
                              on_click=State.open_latest_findings,
                              size="1", variant="soft"),
                ),
                spacing="2", width="100%", min_width="0", align="start",
            ),
            **ASSISTANT_ROW,
        ),
        width="100%", padding="16px 0",
    )

def system_message(item):
    """Служебная плашка по центру (например, «Раздел переключён»)."""
    return rx.box(
        rx.hstack(
            rx.icon("info", size=15, color=MUTED),
            rx.text(item["content"], size="1", color=MUTED, font_style="italic"),
            spacing="2", align="center",
        ),
        width="100%", padding="8px 0", justify_content="center",
    )


# ═══════════════════════════════════════════════════════════════
#  ДИСПЕТЧЕР
# ═══════════════════════════════════════════════════════════════

def message(item):
    """Выбор компонента по роли. Fallback — как у ассистента."""
    return rx.match(
        item["role"],
        ("user",      user_message(item)),
        ("assistant", assistant_message(item)),
        ("system",    system_message(item)),
        assistant_message(item),
    )

# ═══════════════════════════════════════════════════════════════
#  КОМПОНЕНТЫ ВКЛАДКИ
# ═══════════════════════════════════════════════════════════════


def _stream():
    """Скроллируемый список сообщений со спиннером."""
    return rx.auto_scroll(
        rx.vstack(
            rx.foreach(State.messages, message),
            rx.cond(
                State.busy,
                rx.hstack(
                    rx.spinner(size="1"),
                    rx.text(State.agent_status, color=MUTED, size="2"),
                ),
            ),
            **CHAT_STREAM,
        ),
        flex="1", min_height="0", width="100%", overflow_y="auto",
    )


def _structure_hint():
    """Кнопка-переход на вкладку «Разметка»."""
    return rx.button(
        rx.icon("list-tree", size=17),
        rx.text(
            rx.cond(
                State.pending_review,
                "Разметка ждёт проверки",
                "Разметка принята",
            ),
            size="2",
        ),
        rx.spacer(),
        rx.icon("arrow-up-right", size=16),
        on_click=State.set_tab("structure"),
        **STRUCTURE_HINT,
    )


def _composer():
    return rx.form(
        rx.vstack(
            rx.text_area(
                name="user_prompt",
                value=State.user_prompt,
                on_change=State.set_user_prompt,
                disabled=State.busy,
                enter_key_submit=True,
                **COMPOSER_TEXTAREA,
            ),
            rx.hstack(
                rx.cond((State.document_id == "") & (State.pending_upload == ""), _book_picker()),
                rx.spacer(),
                rx.icon_button(
                    "arrow-up",
                    type="submit",
                    aria_label="Отправить сообщение",
                    disabled=State.busy,
                    **SEND_BTN,
                ),
                spacing="2", width="100%", align="center",
            ),
            spacing="2", width="100%", align="stretch",
        ),
        on_submit=State.send,
        reset_on_submit=True,
        **COMPOSER,
    )


# ═══════════════════════════════════════════════════════════════
#  ВКЛАДКА ЦЕЛИКОМ
# ═══════════════════════════════════════════════════════════════

def chat():
    return rx.vstack(
        _stream(),
        rx.vstack(
            rx.cond(State.document_id != "", _structure_hint()),
            rx.cond(State.upload_status != "", _status_block()),
            _composer(),
            rx.text(
                "Ответы могут быть неточными. Сверяй их с исходным текстом.",
                **DISCLAIMER,
            ),
            **CHAT_SHELL,
        ),
        height="100%", width="100%", min_height="0", spacing="0",
    )
