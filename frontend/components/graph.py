"""Вкладка «Подготовка»: подготовка книги к поиску, сцены и персонажи/места.

Технические имена слоёв (NarrativeScene, TextChunk, PRESENT_IN…) спрятаны в
«Подробности»; ручные кнопки отдельных стадий сохранены (CLAUDE.md) — они нужны
для подготовки одной главы и диагностики.
"""
import reflex as rx

from ..state import State
from ..styles import BG, INK, LINE, MUTED, SURFACE


def _details(summary, *children):
    """Свёрнутый блок «Подробности» для диагностики."""
    return rx.el.details(
        rx.el.summary(summary, style={"cursor": "pointer", "color": MUTED,
                                      "font_size": "12px"}),
        rx.vstack(*children, spacing="1", padding_top="6px", align="start"),
        width="100%",
    )


def _hint(text):
    return rx.text(text, size="1", color=MUTED)


# ─── Подготовка ─────────────────────────────────────────────────


def _scope_controls():
    return rx.hstack(
        rx.hstack(
            rx.button(
                "Глава: " + State.selected_chapter_title,
                on_click=State.set_preparation_scope("chapter"),
                variant=rx.cond(State.preparation_scope == "chapter", "solid", "soft"),
                size="2", max_width="360px", overflow="hidden", text_overflow="ellipsis",
                white_space="nowrap",
            ),
            rx.button(
                "Вся книга",
                on_click=State.set_preparation_scope("book"),
                variant=rx.cond(State.preparation_scope == "book", "solid", "soft"),
                size="2",
            ),
            wrap="wrap", spacing="2", align="center",
        ),
        rx.checkbox(
            "Смысловой поиск (эмбеддинги)",
            checked=State.preparation_embeddings,
            on_change=State.set_preparation_embeddings,
            size="2",
        ),
        spacing="3", align="center", width="100%", wrap="wrap",
    )


def _preparation():
    return rx.vstack(
        rx.hstack(
            rx.vstack(
                rx.heading("Подготовка книги", size="4"),
                spacing="1", align="start",
            ),
            rx.spacer(),
            rx.text(State.preparation_summary, size="1", color=MUTED, white_space="nowrap"),
            width="100%", align="start",
        ),
        _scope_controls(),
        rx.hstack(
            rx.button(
                rx.cond(State.busy, rx.spinner(size="1"), rx.icon("play", size=16)),
                rx.cond(State.preparation_failed_chapters.length() > 0,
                        "Повторить", "Подготовить"),
                on_click=State.prepare_graph_scope,
                disabled=State.busy | (State.document_id == ""),
                size="2",
            ),
            rx.text(State.preparation_progress, size="2", color=INK),
            rx.cond(
                State.preparation_active,
                rx.button(
                    rx.icon("square", size=14),
                    rx.cond(State.preparation_cancelling, "Останавливаю…", "Отменить"),
                    on_click=State.cancel_preparation,
                    disabled=State.preparation_cancelling,
                    size="2", variant="soft", color_scheme="red",
                ),
            ),
            align="center", spacing="3", wrap="wrap",
        ),
        rx.cond(
            State.preparation_error != "",
            rx.callout(State.preparation_error, icon="circle-alert", color_scheme="red",
                       size="1", width="100%"),
        ),
        rx.cond(
            State.preparation_status != "",
            rx.text(State.preparation_status, size="1", color=MUTED),
        ),
        _details(
            "Подробности",
            rx.text(State.preparation_details, size="1", color=MUTED),
            rx.cond(State.preparation_error_detail != "",
                    rx.text(State.preparation_error_detail, size="1", color=MUTED,
                            white_space="pre-wrap", overflow_wrap="anywhere")),
        ),
        spacing="3", width="100%", align="stretch", padding="18px",
        border=f"1px solid {LINE}", border_radius="12px", background=SURFACE,
    )


# ─── Глава ──────────────────────────────────────────────────────


_SCENE_DOT = {"ready": "#0ca66f", "partial": "#ed8500", "failed": "#d14343",
              "stale": "#ed8500"}


def _chapter_button(item):
    return rx.button(
        rx.box(
            width="7px", height="7px", border_radius="50%", flex_shrink="0",
            background=rx.match(item["scene_status"],
                                *[(key, color) for key, color in _SCENE_DOT.items()],
                                "#d6d2c8"),
        ),
        rx.text(item["label"], size="2", text_align="left", flex="1"),
        on_click=State.select_section(item["index"]),
        variant=rx.cond(State.selected == item["index"], "soft", "ghost"),
        justify_content="flex-start", width="100%", size="1", align="center",
        white_space="normal", height="auto", padding="7px 9px", gap="8px",
    )


def _chapter_list():
    return rx.vstack(
        rx.text("ГЛАВЫ", size="1", weight="medium", color=MUTED),
        rx.foreach(State.chapters, _chapter_button),
        spacing="1", align="stretch", width="100%",
        max_height="70dvh", overflow_y="auto",
    )


def _entity_row(entity):
    return rx.vstack(
        rx.text(entity["name"], size="2", weight="medium", color=INK),
        rx.text(entity["details"], size="1", color=MUTED),
        spacing="1", align="start", width="100%", padding="8px 0",
        border_bottom=f"1px solid {LINE}",
    )


def _scene_row(scene):
    return rx.button(
        rx.vstack(
            rx.text(scene["title"], size="2", weight="medium", text_align="left"),
            rx.cond(scene["note"] != "", rx.text(scene["note"], size="1", color="#ed8500")),
            spacing="0", align="start",
        ),
        on_click=State.open_scene_editor(scene["id"]),
        variant="ghost", size="1", width="100%", justify_content="flex-start",
        height="auto", padding="6px 8px", white_space="normal",
    )


def _chapter_panel():
    return rx.vstack(
        rx.text("ВЫБРАННАЯ ГЛАВА", size="1", weight="medium", color=MUTED),
        rx.heading(State.selected_chapter_title, size="4"),
        # Сцены
        rx.text("Сцены", size="2", weight="medium", color=INK, padding_top="4px"),
        rx.text(State.scene_status, size="1", color=MUTED),
        rx.cond(
            State.scene_has_manual,
            rx.checkbox("Заменить мои правки сцен", checked=State.replace_human_scenes,
                        on_change=State.set_replace_human_scenes, size="1"),
        ),
        rx.button(
            rx.icon("layers-3", size=16),
            "Построить / обновить сцены",
            on_click=State.build_selected_scenes,
            disabled=State.busy | (State.document_id == "")
            | (State.scene_has_manual & ~State.replace_human_scenes),
            width="100%", size="2", variant="soft",
        ),
        rx.cond(
            State.scene_has_manual & ~State.replace_human_scenes,
            _hint("Сцены исправлены вручную — отметьте «Заменить мои правки сцен», "
                  "чтобы построить заново."),
        ),
        rx.cond(State.scene_issues != "",
                _details("Подробности", rx.text(State.scene_issues, size="1", color=MUTED,
                                                white_space="pre-wrap"))),
        rx.cond(
            State.scene_items.length() > 0,
            rx.vstack(rx.foreach(State.scene_items, _scene_row), spacing="0", width="100%",
                      max_height="30dvh", overflow_y="auto"),
        ),
        rx.divider(),
        # Персонажи и места главы
        rx.text("Персонажи и места главы", size="2", weight="medium", color=INK),
        rx.text(State.entity_status, size="1", color=MUTED),
        rx.button(
            rx.icon("scan-text", size=16),
            "Найти в главе",
            on_click=State.analyze_selected_entities,
            disabled=State.busy | ~State.scenes_ready | (State.document_id == ""),
            width="100%", size="2", variant="soft",
        ),
        rx.cond(~State.scenes_ready, _hint("Сначала постройте сцены этой главы.")),
        rx.cond(
            State.entity_items.length() > 0,
            rx.vstack(rx.foreach(State.entity_items, _entity_row), width="100%", spacing="0"),
        ),
        spacing="2", width="100%", align="stretch",
    )


def _book_panel():
    return rx.vstack(
        rx.text("КНИГА", size="1", weight="medium", color=MUTED),
        rx.heading("Персонажи и места книги", size="4"),
        rx.text(State.book_entity_status, size="1", color=MUTED),
        rx.button(
            rx.icon("git-merge", size=16),
            "Собрать по всей книге",
            on_click=State.resolve_book_entities,
            disabled=State.busy | (State.document_id == ""),
            width="100%", size="2", variant="soft",
        ),
        rx.button(
            rx.icon("refresh-cw", size=16),
            "Проверить повторы",
            on_click=State.reconcile_book_entities,
            disabled=State.busy | (State.document_id == "")
                     | (State.book_entity_items.length() == 0),
            width="100%", size="2", variant="outline",
        ),
        rx.cond(
            State.book_entity_items.length() > 0,
            rx.vstack(rx.foreach(State.book_entity_items, _entity_row),
                      width="100%", spacing="0", max_height="50dvh", overflow_y="auto"),
        ),
        spacing="2", width="100%", align="stretch",
    )


def graph():
    return rx.box(
        rx.vstack(
            _preparation(),
            rx.grid(
                _chapter_list(),
                _chapter_panel(),
                _book_panel(),
                width="100%", gap="28px", align_items="start",
                style={
                    "gridTemplateColumns":
                        "repeat(auto-fit, minmax(min(100%, 280px), 1fr))",
                },
            ),
            spacing="5", width="100%", align="stretch",
            max_width="1280px", margin="0 auto", padding="24px",
        ),
        width="100%", height="100%", overflow_y="auto", background=BG,
    )
