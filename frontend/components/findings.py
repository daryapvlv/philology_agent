"""Вкладка «Находки»: evidence table поисков текущего диалога.

Таблица фрагментов выборки: фильтр по статусу, ручное решение исследователя
(SearchService.review_candidate), переход к главе в «Разметке» и экспорт.
Выборку также меняет агент в «Диалоге» (поиск, расширение, уточнение).
"""
import reflex as rx

from ..state import State
from ..styles import BG, INK, LINE, MUTED


_STATUS_COLOR = (("relevant", "green"), ("rejected", "red"), ("uncertain", "amber"))
_STATUS_OPTIONS = [("relevant", "Подходит"), ("uncertain", "Под вопросом"),
                   ("rejected", "Отклонён"), ("unreviewed", "Не проверен")]


def _search_row(item):
    return rx.button(
        rx.vstack(
            rx.hstack(
                rx.text(item["query"], size="2", weight="medium", color=INK,
                        white_space="normal", text_align="left"),
                rx.spacer(),
                rx.cond(item["current"] != "true",
                        rx.tooltip(rx.badge("книга изменилась", color_scheme="gray", size="1"),
                                   content="После поиска разметка книги менялась — "
                                           "фрагменты могли сместиться.")),
                width="100%", align="center",
            ),
            rx.text(
                item["candidates"] + " фрагментов · подходят " + item["selected"]
                + " · не проверено " + item["unreviewed"] + " · " + item["updated_at"],
                size="1", color=MUTED,
            ),
            spacing="1", align="start", width="100%",
        ),
        on_click=State.open_finding_search(item["id"]),
        variant=rx.cond(State.findings_selected_search == item["id"], "soft", "ghost"),
        justify_content="flex-start", width="100%", size="1",
        white_space="normal", text_align="left", height="auto", padding="10px 12px",
        border_bottom=f"1px solid {LINE}", border_radius="0",
    )


def _filter_chip(chip):
    return rx.button(
        chip["label"] + " · " + chip["count"],
        on_click=State.set_findings_filter(chip["status"]),
        variant=rx.cond(State.findings_filter == chip["status"], "solid", "soft"),
        size="1", color_scheme="gray",
    )


def _status_select(item):
    return rx.select.root(
        rx.select.trigger(variant="soft",
                          color_scheme=rx.match(item["status"], *_STATUS_COLOR, "gray"),
                          width="150px"),
        rx.select.content(*[rx.select.item(label, value=value) for value, label in _STATUS_OPTIONS]),
        value=item["status"],
        on_change=lambda value: State.review_finding(item["id"], value),
        size="1",
        disabled=State.busy,
    )


def _fragment_cell(item):
    return rx.vstack(
        rx.text(rx.cond(item["sections"] != "", item["sections"], "Без раздела"),
                size="1", color=MUTED, weight="medium"),
        rx.text(item["text"], size="2", color=INK, white_space="pre-wrap", line_height="1.6"),
        rx.el.details(
            rx.el.summary("Подробности", style={"cursor": "pointer", "color": MUTED,
                                                "font_size": "12px"}),
            rx.vstack(
                rx.cond(item["context"] != "",
                        rx.text(item["context"], size="1", color=MUTED, white_space="pre-wrap",
                                line_height="1.6")),
                rx.text("Символы книги " + item["start_char"] + "–" + item["end_char"]
                        + " · фрагмент " + item["id"], size="1", color=MUTED),
                spacing="1", padding_top="6px", align="start",
            ),
        ),
        spacing="1", align="start",
    )


def _row(item, index):
    return rx.table.row(
        rx.table.cell(rx.text(index + 1, size="1", color=MUTED)),
        rx.table.cell(_status_select(item)),
        rx.table.cell(_fragment_cell(item)),
        rx.table.cell(rx.text(item["reason"], size="1", color=MUTED, font_style="italic",
                              line_height="1.5")),
        rx.table.cell(rx.text(item["how_found"], size="1", color=MUTED, white_space="pre-line",
                              line_height="1.5")),
        rx.table.cell(rx.icon_button("book-open", size="1", variant="ghost",
                                     on_click=State.show_finding_in_text(item["section_id"]),
                                     disabled=item["section_id"] == "",
                                     aria_label="Показать в тексте",
                                     title="Показать в тексте")),
        vertical_align="top",
    )


def _items_table():
    header = [("№", "36px"), ("Статус", "150px"), ("Фрагмент", None),
              ("Почему", "24%"), ("Как искала", "20%"), ("", "40px")]
    return rx.box(
        rx.table.root(
            rx.table.header(rx.table.row(*[
                rx.table.column_header_cell(title, width=width) if width
                else rx.table.column_header_cell(title) for title, width in header])),
            rx.table.body(rx.foreach(State.findings_items, _row)),
            variant="surface", size="1", width="100%", min_width="900px",
            table_layout="fixed",
        ),
        width="100%", overflow_x="auto",
    )


def _search_list():
    return rx.vstack(
        rx.hstack(
            rx.text("ПОИСКИ ЭТОГО ЧАТА", size="1", weight="medium", color=MUTED),
            rx.spacer(),
            rx.icon_button("refresh-cw", size="1", variant="ghost",
                           on_click=State.refresh_findings,
                           aria_label="Обновить список поисков"),
            width="100%", align="center",
        ),
        rx.cond(
            State.findings_searches.length() > 0,
            rx.vstack(rx.foreach(State.findings_searches, _search_row),
                      width="100%", spacing="0"),
            rx.text(
                rx.cond(State.findings_status != "", State.findings_status,
                        "Поисков пока нет — спросите что-нибудь у ассистента в «Диалоге»."),
                size="2", color=MUTED, padding="10px 4px",
            ),
        ),
        spacing="2", width="100%", align="stretch",
        max_height="75dvh", overflow_y="auto",
    )


def _export_controls():
    return rx.hstack(
        rx.checkbox(
            "Экспортировать все статусы (иначе только «Подходит»)",
            checked=State.findings_include_all,
            on_change=State.set_findings_include_all,
            size="1",
        ),
        rx.spacer(),
        rx.button(rx.icon("sheet", size=14), "CSV", size="1", variant="soft",
                  on_click=State.download_findings_csv),
        rx.button(rx.icon("file-down", size=14), "Markdown", size="1", variant="soft",
                  on_click=State.download_findings_markdown),
        rx.button(rx.icon("file-json", size=14), "JSON", size="1", variant="soft",
                  on_click=State.download_findings_json),
        width="100%", align="center", wrap="wrap", spacing="2",
    )


def _table():
    return rx.cond(
        State.findings_selected_search != "",
        rx.vstack(
            rx.heading(State.findings_query, size="4"),
            rx.hstack(rx.foreach(State.findings_counts, _filter_chip), wrap="wrap", spacing="2"),
            _hint_line(),
            rx.cond(
                State.findings_items.length() > 0,
                _items_table(),
                rx.text(rx.cond(State.findings_status != "", State.findings_status, "Загрузка…"),
                        size="2", color=MUTED),
            ),
            rx.cond(
                State.findings_next_offset != "",
                rx.button("Показать ещё", size="1", variant="ghost",
                          on_click=State.load_more_findings, width="100%"),
            ),
            _export_controls(),
            spacing="3", width="100%", align="stretch",
        ),
        rx.text("Выберите поиск слева, чтобы увидеть найденные фрагменты.",
                size="2", color=MUTED, padding="10px 4px"),
    )


def _hint_line():
    return rx.text("Статус можно изменить — это ваше решение, оно попадёт в экспорт. "
                   "Причина модели сохраняется рядом.", size="1", color=MUTED)


def findings():
    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.vstack(
                    rx.text("НАХОДКИ", size="1", weight="medium", color=MUTED),
                    rx.heading("Таблица найденных фрагментов", size="5"),
                    spacing="1", align="start",
                ),
                rx.spacer(),
                rx.button(rx.cond(State.findings_list_hidden,
                                  rx.icon("panel-left-open", size=14),
                                  rx.icon("panel-left-close", size=14)),
                          rx.cond(State.findings_list_hidden, "Показать поиски", "Скрыть поиски"),
                          size="1", variant="ghost", on_click=State.toggle_findings_list),
                width="100%", align="end",
            ),
            rx.flex(
                rx.cond(~State.findings_list_hidden,
                        rx.box(_search_list(), flex="0 0 260px", min_width="220px")),
                rx.box(_table(), flex="1 1 600px", min_width="0"),
                width="100%", gap="20px", align="start", wrap="wrap",
            ),
            spacing="4", width="100%", align="stretch",
            margin="0 auto", padding="20px 24px",
        ),
        width="100%", height="100%", overflow_y="auto", background=BG,
    )
