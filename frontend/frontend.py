"""Корень приложения: workspace, index, app."""
import reflex as rx

from .state import State
from .styles import BG, INK, MUTED, LINE, PAD_SHELL, VIEWPORT_H, ACCENT
from .components.sidebar import sidebar
from .components.chat import chat
from .components.structure import scene_editor, structure
from .components.graph import graph
from .components.findings import findings
from .components.welcome import welcome


def workspace():
    return rx.tabs.root(
        rx.hstack(
            rx.cond(
                State.sidebar_hidden,
                rx.icon_button(
                    "panel-left-open", variant="ghost",
                    on_click=State.toggle_sidebar,
                    aria_label="Показать список чатов",
                    flex_shrink="0",
                ),
            ),
            rx.vstack(
                rx.text(
                    rx.cond(State.document_id != "", State.book, "Без книги"), size="2", weight="medium",
                    max_width="350px",
                    white_space="nowrap", overflow="hidden",
                    text_overflow="ellipsis",
                ),
                rx.text(
                    rx.cond(
                        State.tab == "structure",
                        rx.cond(State.pending_review, "Разметка ждёт проверки",
                                "Разметка принята"),
                        rx.cond(State.tab == "graph", State.preparation_summary,
                                rx.cond(State.tab == "findings", State.findings_query,
                                        State.chat_title)),
                    ),
                    size="1", color=MUTED,
                ),
                spacing="1", min_width="0",
            ),
            rx.spacer(),
            rx.tabs.list(
                rx.tabs.trigger("Диалог", value="chat"),
                rx.tabs.trigger("Разметка", value="structure", disabled=State.document_id == ""),
                rx.tabs.trigger("Подготовка", value="graph", disabled=State.document_id == ""),
                rx.tabs.trigger("Находки", value="findings", disabled=State.document_id == ""),
                background="#f1f0ec", padding="3px", border_radius="7px",
            ),
            width="100%", padding="10px 18px", background="#fffefb",
            border_bottom=f"1px solid {LINE}", align="center",
        ),
        rx.tabs.content(chat(),      value="chat",      flex="1", min_height="0", padding="0"),
        rx.tabs.content(structure(), value="structure", flex="1", min_height="0", padding="0"),
        rx.tabs.content(graph(),     value="graph",     flex="1", min_height="0", padding="0"),
        rx.tabs.content(findings(),  value="findings",  flex="1", min_height="0", padding="0"),
        scene_editor(),
        value=State.tab, on_change=State.set_tab,
        display="flex", flex_direction="column",
        flex="1", min_height="0", width="100%",
    )

def _busy_indicator():
    return rx.cond(
        State.busy,
        rx.box(
            height="2px", width="100%",
            background=ACCENT,
            position="fixed", top="0", left="0", z_index="100",
        ),
    )

def index():
    return rx.hstack(
        rx.cond(~State.sidebar_hidden, sidebar()),
        rx.vstack(
            rx.cond(State.database_error != "",
                    rx.callout(State.database_error, color_scheme="orange", margin="12px 24px")),
            rx.cond(
                State.error != "",
                rx.callout(
                    rx.hstack(
                        rx.text(State.error, flex="1", overflow_wrap="anywhere"),
                        rx.icon_button("x", on_click=State.clear_error, size="1",
                                       variant="ghost", color_scheme="red",
                                       aria_label="Закрыть сообщение об ошибке"),
                        align="center", width="100%",
                    ),
                    icon="circle-alert", color_scheme="red", margin="12px 24px",
                ),
            ),
            rx.cond((State.messages.length() == 0) & (State.document_id == ""), welcome(), workspace()),
            flex="1", height=VIEWPORT_H, min_width="0",
            spacing="0", align="stretch",
        ),
        spacing="0", width="100%", height=VIEWPORT_H,
        overflow="hidden", background=BG, color=INK,
    )


app = rx.App()
app.add_page(
    index,
    title="Филолог · Чаты по книгам",
    on_load=State.load,
)
