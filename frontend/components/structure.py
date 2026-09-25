"""Вкладка «Структура»: оглавление, excerpt, drawer правки."""
import reflex as rx

from structure.rules import HEADING_ROLES
from ..state import State
from ..styles import (
    MUTED,
    SECTION_BTN, STRUCTURE_GRID, STRUCTURE_SHELL,
    EXCERPT_TEXT,MARKUP_CANVAS, MARKUP_PAPER, 
    MARKUP_SIDEBAR, MARKUP_PANEL_LABEL,
    MARKUP_DIVIDER, MARKUP_ISSUE_CARD, MARKUP_FRAGMENT,
    MARKUP_ROOT_STYLE,
)


ROLE_OPTIONS = [
    (label, role) for role, label in {
        "collection": "Сборник", "work": "Произведение", "volume": "Том",
        "part": "Часть", "chapter": "Глава", "section": "Раздел",
        "paragraph": "Параграф", "preface": "Предисловие", "prologue": "Пролог",
        "epilogue": "Эпилог", "appendix": "Приложение", "act": "Действие",
        "picture": "Картина", "dramatic_scene": "Явление",
        "dramatis_personae": "Действующие лица", "other": "Другой раздел",
    }.items() if role in HEADING_ROLES
]


CAPTURE_SELECTION_JS = """(() => {
  const root = document.getElementById('book-selection-reader');
  if (!root) return {valid:false, silent:true};
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0) return {valid:false, silent:true};
  const range = selection.getRangeAt(0);
  if (selection.isCollapsed) {
    return {valid:false, silent:true};
  }
  if (!root.contains(range.commonAncestorContainer)) {
    return {valid:false, silent:true};
  }
  const owner = (node) => {
    const el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    return el?.closest?.('[data-book-element]');
  };
  const first = owner(range.startContainer);
  const last = owner(range.endContainer);
  if (!first || !last || !root.contains(first) || !root.contains(last)) {
    return {valid:false, silent:true};
  }
  const start = Number(first.dataset.bookElement);
  const end = Number(last.dataset.bookElementEnd)
    || Number(last.dataset.bookElement) + 1;
  if (!Number.isInteger(start) || !Number.isInteger(end) || start >= end) {
    return {valid:false, silent:true};
  }
  const rect = range.getBoundingClientRect();
  const width = 430;
  const x = Math.max(12, Math.min(window.innerWidth - width - 12,
    rect.left + rect.width / 2 - width / 2));
  const y = rect.top > 70 ? rect.top - 52 : rect.bottom + 12;
  return {valid:true, start, end, x:Math.round(x), y:Math.round(y)};
})()"""

READ_SELECTION_JS = """(() => {
  const selection = window.getSelection();
  const root = document.getElementById('book-selection-reader');
  if (!root) {
    return {valid:false, message:'Текст выбранного раздела не найден.'};
  }
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
    const saved = window.__bookContextSelection;
    if (saved && Number.isInteger(saved.start) && Number.isInteger(saved.end)
        && saved.start < saved.end) {
      return {valid:true, start:saved.start, end:saved.end};
    }
    return {valid:false, message:'Выделите текст или откройте меню на нужном блоке.'};
  }
  const range = selection.getRangeAt(0);
  if (!root.contains(range.commonAncestorContainer)) {
    return {valid:false, message:'Выделяйте текст только внутри текущего раздела.'};
  }
  const owner = (node) => {
    const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    return element?.closest?.('[data-book-element]');
  };
  const first = owner(range.startContainer);
  const last = owner(range.endContainer);
  if (!first || !last || !root.contains(first) || !root.contains(last)) {
    return {valid:false, message:'Выделение должно начинаться и заканчиваться внутри текста.'};
  }
  const start = Number(first.dataset.bookElement);
  const end = Number(last.dataset.bookElement) + 1;
  if (!Number.isInteger(start) || !Number.isInteger(end) || start >= end) {
    return {valid:false, message:'Не удалось определить границы выделения.'};
  }
  const normalized = document.createRange();
  normalized.selectNodeContents(first);
  normalized.setEnd(last, last.childNodes.length);
  selection.removeAllRanges();
  selection.addRange(normalized);
  const rect = normalized.getBoundingClientRect();
  const width = 430;
  const x = Math.max(12, Math.min(window.innerWidth - width - 12,
    rect.left + rect.width / 2 - width / 2));
  const y = rect.top > 70 ? rect.top - 52 : rect.bottom + 12;
  return {valid:true, start, end, x:Math.round(x), y:Math.round(y)};
})()"""

CAPTURE_CONTEXT_TARGET_JS = """(() => {
  const root = document.getElementById('book-selection-reader');
  const row = root?.querySelector('.markup-fragment:hover');
  const element = row?.querySelector('[data-book-element]');
  const position = Number(element?.dataset?.bookElement);
  window.__bookContextSelection = Number.isInteger(position)
    ? {start: position, end: position + 1}
    : null;
})()"""


# ═══════════════════════════════════════════════════════════════
#  АТОМЫ
# ═══════════════════════════════════════════════════════════════

def section_button(item):
    return rx.button(
        item["label"],
        on_click=State.select_section(item["index"]),
        variant=rx.cond(State.selected == item["index"], "soft", "ghost"),
        **SECTION_BTN,
        padding_left=item["indent"],
    )

# ═══════════════════════════════════════════════════════════════
#  ЧАСТИ ВКЛАДКИ
# ═══════════════════════════════════════════════════════════════


def _selection_action(label, operation, icon, *, context=False):
    if context:
        return rx.cond(
            State.selected_blocks.length() > 0,
            rx.context_menu.item(
                rx.icon(icon, size=14), label,
                on_select=State.apply_selected_blocks(operation),
            ),
            rx.context_menu.item(
                rx.icon(icon, size=14), label,
                on_select=rx.call_script(
                    READ_SELECTION_JS,
                    callback=lambda result: State.apply_text_selection(result, operation),
                ),
            ),
        )
    return rx.button(
        rx.icon(icon, size=14), label,
        on_click=State.apply_captured_selection(operation),
        size="1", variant="ghost", color="white",
    )


def _floating_selection_menu():
    return rx.cond(
        State.selection_active & ~State.text_context_menu_open,
        rx.hstack(
            _selection_action("Эпиграф", "epigraph", "quote"),
            _selection_action("Источник", "attribution", "signature"),
            _selection_action("Основной текст", "body", "text-cursor-input"),
            rx.icon_button(
                "x", on_click=State.clear_text_selection,
                variant="ghost", color="white", size="1",
                aria_label="Закрыть меню выделения",
            ),
            rx.button(
                rx.icon("merge", size=14), "Объединить",
                on_click=State.merge_selection,
                size="1", variant="ghost", color="white",
            ),
            rx.button(
                rx.icon("split", size=14), "Разъединить",
                on_click=State.unmerge_selection,
                size="1", variant="ghost", color="white",
            ),
            position="fixed", left=State.selection_left, top=State.selection_top,
            z_index="50", padding="5px", spacing="1", align="center",
            background="#292824", border_radius="10px",
            box_shadow="0 8px 28px #0003", max_width="calc(100vw - 24px)",
            flex_wrap="wrap",

        ),
    )


def _operation_notice():
    return rx.cond(
        State.selection_notice != "",
        rx.callout(
            State.selection_notice,
            icon="circle-check",
            color_scheme="green",
            size="1",
            position="fixed",
            right="20px",
            bottom="20px",
            z_index="100",
            width="fit-content",
            max_width="min(380px, calc(100vw - 40px))",
            box_shadow="0 10px 32px #29282424",
            pointer_events="none",
        ),
    )


def _block_action_panel():
    def role_button(label, operation, icon, color):
        return rx.button(
            rx.icon(icon, size=15), label,
            on_click=State.apply_selected_blocks(operation),
            size="1", variant="ghost", color=color,
            width="100%", justify_content="flex-start",
        )

    return rx.cond(
        State.selected_blocks.length() > 0,
        rx.vstack(
            rx.hstack(
                rx.text("Тип блока", size="1", weight="bold", color="#403e39"),
                rx.spacer(),
                rx.icon_button(
                    "x", aria_label="Снять выбор",
                    on_click=State.clear_block_selection,
                    size="1", variant="ghost", color_scheme="gray",
                ),
                width="100%", align="center",
            ),
            role_button("Эпиграф", "epigraph", "quote", "#c96e00"),
            role_button("Источник", "attribution", "signature", "#127f99"),
            role_button("Вступление", "opening", "book-open-text", "#7046ce"),
            role_button("Основной текст", "body", "text-cursor-input", "#07865a"),
            role_button("Примечание", "footnotes", "sticky-note", "#9a5b24"),
            rx.divider(margin="2px 0"),
            rx.cond(
                State.selected_blocks.length() >= 2,
                rx.button(
                    rx.icon("merge", size=15), "Объединить блоки",
                    on_click=State.merge_selected_blocks,
                    size="1", variant="ghost", width="100%",
                    justify_content="flex-start",
                ),
            ),
            rx.button(
                rx.icon("between-horizontal-start", size=15), "Создать раздел…",
                on_click=State.open_section_editor,
                size="1", variant="ghost", width="100%",
                justify_content="flex-start",
            ),
            position="fixed", top="112px", right="334px", z_index="60",
            width="210px", padding="10px", spacing="1",
            background="#fffefb", border="1px solid #ded9cf",
            border_radius="10px", box_shadow="0 10px 32px #29282424",
            style={"@media (max-width: 980px)": {"right": "20px"}},
        ),
    )


def _context_menu(reader):
    return rx.context_menu.root(
        rx.context_menu.trigger(reader),
        rx.context_menu.content(
            rx.context_menu.label("Тип блока"),
            _selection_action("Эпиграф", "epigraph", "quote", context=True),
            _selection_action("Источник", "attribution", "signature", context=True),
            _selection_action("Вступление", "opening", "book-open-text", context=True),
            _selection_action("Основной текст", "body", "text-cursor-input",
                              context=True),
            _selection_action("Примечание", "footnotes", "sticky-note", context=True),
            rx.context_menu.separator(),
            rx.context_menu.label("Абзацы"),
            rx.cond(
                State.selected_blocks.length() >= 2,
                rx.context_menu.item(
                    rx.icon("square-check", size=14), "Объединить выбранные блоки",
                    on_select=State.merge_selected_blocks,
                ),
                rx.context_menu.item(
                    rx.icon("merge", size=14), "Объединить абзацы",
                    on_select=rx.call_script(
                        READ_SELECTION_JS,
                        callback=State.merge_selection,
                    ),
                ),
            ),
            rx.context_menu.item(
                rx.icon("split", size=14), "Разъединить блок",
                on_select=rx.call_script(
                    READ_SELECTION_JS,
                    callback=State.unmerge_selection,
                ),
            ),
            rx.context_menu.separator(),
            rx.context_menu.label("Текущий раздел"),
            rx.context_menu.item(
                rx.icon("settings-2", size=14),
                rx.cond(
                    State.selected_blocks.length() > 0,
                    "Создать раздел из выбранного…",
                    "Изменить роль и границы…",
                ),
                on_select=State.open_section_editor,
            ),
            size="2",
        ),
        on_open_change=State.set_text_context_menu_open,
    )


def _selectable_reader():
    def paragraph(item):
        role_class = rx.match(
            item["role"],
            ("epigraph", "markup-fragment fragment-epigraph"),
            ("attribution", "markup-fragment fragment-attribution"),
            ("body", "markup-fragment fragment-body"),
            ("footnotes", "markup-fragment fragment-opening"),
            "markup-fragment fragment-opening",
        )
        role_color = rx.match(
            item["role"],
            ("epigraph", "#ed8500"),
            ("attribution", "#1698b7"),
            ("body", "#0ca66f"),
            ("footnotes", "#9a5b24"),
            "#8357e8",
        )
        hover_background = rx.match(
            item["role"],
            ("epigraph", "#fff7e8"),
            ("attribution", "#eefafd"),
            ("body", "#eefaf5"),
            ("footnotes", "#fff7ed"),
            "#f6f1ff",
        )
        text = rx.el.span(
            item["text"],
            style={"white_space": "pre-line"},
            custom_attrs={
                "data-book-element": item["position"],
                "data-element-length": item["length"],
                "data-book-element-end": item["end_position"],
            },
        )
        is_selected = State.selected_blocks.contains(item["position"])
        return rx.fragment(
            rx.box(
                rx.el.button(
                    rx.cond(
                        is_selected,
                        rx.icon("check", size=13, stroke_width=3),
                    ),
                    on_click=State.toggle_block(item["position"]),
                    on_mouse_up=rx.stop_propagation,
                    type="button",
                    class_name="block-selector",
                    aria_label=rx.cond(is_selected, "Снять выбор блока", "Выбрать блок"),
                    custom_attrs={"aria-pressed": is_selected},
                ),
                rx.cond(
                    (item["role"] == "epigraph") | (item["group_start"] == "true"),
                    rx.hstack(
                        rx.text(
                            item["role_label"],
                            size="1", weight="medium", color=role_color,
                            letter_spacing="0.02em",
                        ),
                        spacing="1", align="center", margin_bottom="7px",
                    ),
                ),
                rx.cond(
                    State.show_element_numbers,
                    rx.text(
                        item["marker"],
                        size="1", color=MUTED, font_family="system-ui, sans-serif",
                        position="absolute", right="calc(100% + 18px)", top="5px",
                        width="130px", text_align="right",
                        white_space="nowrap", letter_spacing="0.04em",
                    ),
                ),
                rx.box(
                    text,
                    margin_left=item["inset"],
                    margin_right=item["inset"],
                    font_style=rx.cond(item["role"] == "epigraph", "italic", "normal"),
                    text_align=rx.cond(item["role"] == "attribution", "right", "left"),
                    color=rx.cond(item["role"] == "opening", MUTED, "#302f2b"),
                    text_indent=rx.cond(item["role"] == "body", "1.5em", "0"),
                    padding_bottom=rx.cond(item["role"] == "body", "10px", "0"),
                ),
                class_name=role_class,
                id="book-element-" + item["position"],
                color=role_color,
                custom_attrs={"data-selected": is_selected},
                _hover={
                    "background": hover_background,
                    "& .block-selector": {"opacity": "1", "transform": "scale(1)"},
                },
                **MARKUP_FRAGMENT,
            ),
        )
    reader = rx.box(
        rx.foreach(State.selectable_elements, paragraph),
        id="book-selection-reader",
        tab_index=0,
        aria_label="Текст раздела. Выделите фрагмент, чтобы исправить структуру.",
        on_context_menu=rx.call_script(CAPTURE_CONTEXT_TARGET_JS),
        on_mouse_up=rx.call_script(
            CAPTURE_SELECTION_JS,
            callback=State.capture_text_selection,
        ),
        color="#302f2b",
        **EXCERPT_TEXT,
    )
    return _context_menu(reader)


def _section_editor():
    role_options = [
        rx.el.option(label, value=role)
        for label, role in ROLE_OPTIONS
    ]
    field_style = {
        "width": "100%", "border": "1px solid #d5d1c8",
        "border_radius": "7px", "background": "white",
        "padding": "8px 10px", "color": "#292824",
    }
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title(
                rx.cond(
                    State.section_editor_mode == "create",
                    "Новый раздел",
                    "Роль и границы раздела",
                ),
            ),
            rx.dialog.description(
                rx.cond(
                    State.section_editor_mode == "create",
                    "Выбранный диапазон станет новым разделом; исходный раздел сохранится.",
                    "Начало входит в раздел, конец — нет. Под каждым полем показан текст соответствующего блока.",
                ),
                line_height="1.5",
            ),
            rx.cond(
                State.selection_error != "",
                rx.callout(
                    State.selection_error, icon="circle-alert",
                    color_scheme="orange", size="1", width="100%",
                ),
            ),
            rx.form(
                rx.vstack(
                    rx.vstack(
                        rx.text("Название", size="2", weight="medium"),
                        rx.input(
                            name="title", default_value=State.section_edit_title,
                            placeholder="Без названия", width="100%",
                        ),
                        spacing="1", width="100%", align="start",
                    ),
                    rx.vstack(
                        rx.text("Роль", size="2", weight="medium"),
                        rx.el.select(
                            *role_options,
                            name="role",
                            default_value=State.section_edit_role,
                            **field_style,
                        ),
                        spacing="1", width="100%", align="start",
                    ),
                    rx.grid(
                        rx.vstack(
                            rx.text("Начало", size="2", weight="medium"),
                            rx.input(
                                name="start", type="number",
                                value=State.section_edit_start,
                                on_change=State.set_section_edit_start,
                                debounce_timeout=250,
                                required=True, width="100%",
                            ),
                            rx.text(
                                State.section_edit_start_preview,
                                size="1", color=MUTED, line_height="1.35",
                                min_height="34px",
                            ),
                            spacing="1", align="start",
                        ),
                        rx.vstack(
                            rx.text("Конец", size="2", weight="medium"),
                            rx.input(
                                name="end", type="number",
                                value=State.section_edit_end,
                                on_change=State.set_section_edit_end,
                                debounce_timeout=250,
                                placeholder="Не определён", width="100%",
                            ),
                            rx.text(
                                State.section_edit_end_preview,
                                size="1", color=MUTED, line_height="1.35",
                                min_height="34px",
                            ),
                            spacing="1", align="start",
                        ),
                        columns="2", gap="3", width="100%",
                    ),
                    rx.cond(
                        State.selected_blocks.length() > 0,
                        rx.button(
                            rx.icon("scan-line", size=14),
                            "Взять границы выбранных блоков",
                            type="button", variant="soft", size="1",
                            on_click=State.use_selected_section_bounds,
                        ),
                    ),
                    rx.grid(
                        rx.vstack(
                            rx.text("Позиция заголовка", size="2", weight="medium"),
                            rx.input(
                                name="title_position", type="number",
                                default_value=State.section_edit_title_position,
                                placeholder="Нет", width="100%",
                            ),
                            spacing="1", align="start",
                        ),
                        rx.vstack(
                            rx.text("Уровень", size="2", weight="medium"),
                            rx.input(
                                name="level", type="number", min_=1,
                                default_value=State.section_edit_level,
                                required=True, width="100%",
                            ),
                            spacing="1", align="start",
                        ),
                        columns="2", gap="3", width="100%",
                    ),
                    rx.cond(
                        State.section_editor_mode == "edit",
                        rx.callout(
                            "Изменение границ сбросит разметку начала раздела и потребует обновить сцены.",
                            icon="info", color_scheme="orange", size="1", width="100%",
                        ),
                    ),
                    rx.hstack(
                        rx.dialog.close(
                            rx.button("Отмена", type="button", variant="soft", color_scheme="gray"),
                        ),
                        rx.button("Сохранить", type="submit"),
                        justify="end", width="100%", spacing="2",
                    ),
                    spacing="4", width="100%",
                ),
                on_submit=State.apply_section_edit,
                reset_on_submit=False,
            ),
            max_width="520px",
        ),
        open=State.section_editor_open,
        on_open_change=State.set_section_editor_open,
    )


def _element_pagination():
    return rx.cond(
        State.selectable_total > 120,
        rx.hstack(
            rx.icon_button(
                "chevron-left", aria_label="Предыдущие блоки",
                on_click=State.change_element_page(-1),
                disabled=~State.can_previous_elements,
                size="1", variant="ghost",
            ),
            rx.text(State.element_page_label, size="1", color=MUTED),
            rx.icon_button(
                "chevron-right", aria_label="Следующие блоки",
                on_click=State.change_element_page(1),
                disabled=~State.can_next_elements,
                size="1", variant="ghost",
            ),
            justify="center", align="center", width="100%", spacing="2",
            padding="4px 0 8px",
        ),
    )


def scene_editor():
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title("Правка сцены"),
            rx.dialog.description(State.scene_range),
            rx.form(rx.vstack(
                rx.input(name="title", default_value=State.scene_title, placeholder="Название", width="100%"),
                rx.text_area(name="summary", default_value=State.scene_summary, width="100%", min_height="120px"),
                rx.input(name="operation", value="edit", type="hidden"),
                rx.button("Сохранить описание", type="submit"),
                width="100%",
            ), on_submit=State.apply_scene),
            rx.divider(margin="16px 0"),
            rx.form(rx.vstack(
                rx.text("Границы задаются в символах исходного текста книги.", size="2"),
                rx.select(["Разделить сцену", "Объединить с правой сценой", "Перенести правую границу"], name="operation", default_value="Разделить сцену"),
                rx.input(name="boundary", placeholder="Новая граница (для разделения или переноса)", type="number", width="100%"),
                rx.button("Применить границы", type="submit"),
                width="100%",
            ), on_submit=State.apply_scene),
            rx.dialog.close(rx.button("Закрыть", variant="soft", margin_top="16px")),
        ),
        open=State.scene_editor_open, on_open_change=State.set_scene_editor_open,
    )


# ═══════════════════════════════════════════════════════════════
#  ВКЛАДКА ЦЕЛИКОМ
# ═══════════════════════════════════════════════════════════════

def _review_card(item):
    card_color = rx.match(
        item["block_type"],
        ("heading", "#478ce7"),
        ("epigraph", "#ed8500"),
        ("body", "#0ca66f"),
        ("opening", "#8357e8"),
        ("attribution", "#1698b7"),
        "#9a7b34",
    )
    card_background = rx.match(
        item["block_type"],
        ("heading", "#f2f7ff"),
        ("epigraph", "#fff7e8"),
        ("body", "#eefaf5"),
        ("opening", "#f6f1ff"),
        ("attribution", "#eefafd"),
        "#fffaf0",
    )
    type_label = rx.match(
        item["block_type"],
        ("heading", "Заголовок"),
        ("epigraph", "Эпиграф"),
        ("body", "Основной текст"),
        ("opening", "Начало главы"),
        ("attribution", "Источник / сноска"),
        "Общее",
    )
    content = rx.vstack(
        rx.hstack(
            rx.box(width="7px", height="7px", border_radius="50%",
                   background="#f59e0b", flex_shrink="0"),
            rx.text(
                rx.cond(item["section_title"] != "", item["section_title"], "Общее замечание"),
                size="1", weight="medium", color="#302f2b",
            ),
            rx.spacer(),
            rx.text(type_label, size="1", color=card_color),
            width="100%", align="center", spacing="2",
        ),
        rx.text(
            item["message"], size="1", color="#55524c", line_height="1.5",
            white_space="normal", overflow_wrap="anywhere", width="100%",
        ),
        spacing="1", align="start", width="100%",
    )
    return rx.cond(
        (item["index"] != "") & (item["position"] != ""),
        rx.button(
            content,
            on_click=State.select_review_item(
                item["index"], item["position"], item["end_position"], item["block_type"],
            ),
            style={"background": card_background, "border-color": card_color},
            **MARKUP_ISSUE_CARD,
        ),
        rx.box(
            content,
            style={"background": card_background, "border-color": card_color},
            **MARKUP_ISSUE_CARD,
        ),
    )

def _markup_document():
    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.text("НАЗВАНИЕ ГЛАВЫ", size="1", color="#478ce7",
                        border="1px solid #478ce7", border_radius="3px",
                        padding="1px 4px"),
                width="100%", align="center",
            ),
            rx.heading(
                rx.cond(State.title != "", State.title, "Исходный текст"),
                id="book-chapter-heading",
                class_name="fragment-heading", size="4", font_family="Georgia, serif",
                font_weight="400", text_align="center", color="#66625b",
                width="100%", padding="8px", border_radius="6px",
                transition="background .16s ease, border-color .16s ease",
            ),
            rx.box(width="42px", height="1px", background="#dedbd3", margin="0 auto 20px"),
            rx.cond(
                State.selection_error != "",
                rx.callout(
                    rx.hstack(
                        rx.text(State.selection_error, flex="1"),
                        rx.icon_button(
                            "x", on_click=State.clear_selection_error,
                            aria_label="Закрыть предупреждение", size="1",
                            variant="ghost", color_scheme="orange",
                        ),
                        align="center", width="100%",
                    ),
                    icon="circle-alert", color_scheme="orange", size="1", width="100%",
                ),
            ),
            _element_pagination(),
            rx.cond(
                State.selectable_elements.length() > 0,
                _selectable_reader(),
                rx.text(State.excerpt, **EXCERPT_TEXT),
            ),
            spacing="2", width="100%", align="stretch",
        ),
        _floating_selection_menu(),
        **MARKUP_PAPER,
    )


def _analysis_summary():
    return rx.vstack(
        rx.hstack(
            rx.text("РАЗМЕТКА ГЛАВЫ", **MARKUP_PANEL_LABEL),
            rx.spacer(),
            rx.text(
                rx.cond(State.pending_review, "Ждёт проверки", "Принята"),
                size="1", color=rx.cond(State.pending_review, "#ed8500", "#0ca66f"),
                background=rx.cond(State.pending_review, "#fff3dc", "#e4f8ee"),
                padding="2px 7px", border_radius="10px",
            ),
            width="100%", align="center",
        ),
        rx.box(
            rx.vstack(
                rx.hstack(
                    rx.text("Блоков в главе", size="1", color="#7656c9"),
                    rx.spacer(),
                    rx.text(State.selectable_total, weight="bold", size="1",
                            color="#7656c9"),
                    width="100%",
                ),
                rx.hstack(
                    rx.box(width="6px", height="6px", border_radius="50%",
                           background="#f59e0b"),
                    rx.text("Требуют проверки", size="1", color=MUTED),
                    rx.spacer(),
                    rx.text(State.review_targets.length(), size="1", color="#ed8500",
                            border="1px solid #f59e0b", border_radius="50%",
                            min_width="18px", height="18px", text_align="center"),
                    width="100%", align="center", spacing="2",
                ),
                spacing="2", width="100%",
            ),
            padding="10px", background="#f8f4ff", border="1px solid #e4d8ff",
            border_radius="7px", width="100%",
        ),
        spacing="3", width="100%",
    )


def _review_sidebar():
    return rx.vstack(
        _analysis_summary(),
        rx.divider(**MARKUP_DIVIDER),
        rx.text("СОДЕРЖАНИЕ", **MARKUP_PANEL_LABEL),
        rx.box(
            rx.cond(
                State.sections.length() > 0,
                rx.vstack(
                    rx.foreach(State.sections, section_button),
                    spacing="0", width="100%",
                ),
                rx.text("Разделы не найдены", size="1", color=MUTED, padding="8px"),
            ),
            height="clamp(180px, 32vh, 360px)", min_height="180px",
            flex_shrink="0", overflow_y="auto", width="100%",
            border="1px solid #e7e4dc", border_radius="7px",
            background="white", padding="4px",
        ),
        rx.divider(**MARKUP_DIVIDER),
        rx.hstack(
            rx.text("НА ПРОВЕРКЕ", **MARKUP_PANEL_LABEL),
            rx.spacer(),
            rx.text(State.review_targets.length(), size="1", color="#ed8500"),
            width="100%",
        ),
        rx.cond(
            State.review_targets.length() > 0,
            rx.vstack(rx.foreach(State.review_targets, _review_card),
                      spacing="2", width="100%"),
            rx.text("Замечаний нет", size="1", color=MUTED),
        ),
        rx.button(
            rx.icon("undo-2", size=16),
            rx.cond(
                State.can_undo_structure,
                "Отменить последнее изменение",
                "Нет изменений для отмены",
            ),
            on_click=State.undo_structure,
            disabled=State.busy | ~State.can_undo_structure | (State.document_id == ""),
            width="100%", variant="soft", color_scheme="gray", size="2",
        ),
        rx.button(
            rx.icon("check", size=16),
            rx.cond(State.pending_review, "Принять разметку", "Разметка принята"),
            on_click=State.approve,
            disabled=State.busy | ~State.pending_review | (State.document_id == ""),
            width="100%", background="#16b876", color="white", size="2",
        ),
        rx.button(
            rx.icon("layers-3", size=16),
            "Сцены и подготовка — во вкладке «Подготовка»",
            on_click=State.set_tab("graph"),
            width="100%", variant="ghost", size="1", color_scheme="gray",
        ),
        spacing="3", align="stretch", **MARKUP_SIDEBAR,
    )


def structure():
    return rx.box(
        rx.vstack(
            rx.grid(
                rx.box(_markup_document(),
                        id="book-reader-scroll",
                        **MARKUP_CANVAS),
                _review_sidebar(),
                **STRUCTURE_GRID,
            ),
            **STRUCTURE_SHELL,
        ),
        _section_editor(),
        _block_action_panel(),
        _operation_notice(),
        height="100%", overflow_y="auto", width="100%",
        style=MARKUP_ROOT_STYLE,
    )
