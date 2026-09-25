"""Единый файл стилей: токены + стили компонентов.

Правила:
  • Только статические значения. Никаких State, rx.cond — они в компонентах.
  • Токены сверху, стили компонентов снизу, сгруппированы.
  • Стиль компонента — плоский dict, передаётся через **kwargs.
"""

# ══════════════════════════════════════════════════════════════
#  ТОКЕНЫ
# ══════════════════════════════════════════════════════════════

# ── палитра ──────────────────────────────────────────────────
BG          = "#faf9f6"
INK         = "#292824"
MUTED       = "#807c74"
LINE        = "#e6e3dc"

SURFACE     = "#f0eee8"
SURFACE_ALT = "#eeece6"
HOVER_BG    = "#e7e4dc"
BORDER_SOFT = "#d5d1c8"

ACCENT      = "#e87912"
WARN        = "#e87912"

DOT_ON      = "#68816a"
DOT_OFF     = "#a29b8e"

OVERLAY     = "#0005"
SHADOW_SOFT = "0 4px 24px #29282408"

# ── типографика ──────────────────────────────────────────────
SERIF = "Georgia, serif"

# ── радиусы ──────────────────────────────────────────────────
R_SM   = "8px"
R_MD   = "12px"
R_LG   = "14px"
R_XL   = "22px"
R_PILL = "50%"

# ── размеры ──────────────────────────────────────────────────
SIDEBAR_W  = "272px"
CHAT_MAX_W = "760px"
BOOK_MAX_W = "1120px"

# ── высоты ───────────────────────────────────────────────────
VIEWPORT_H = "100dvh"
TOC_MAX_H  = "65dvh"
TEXT_MAX_H = "60dvh"

# ── отступы ──────────────────────────────────────────────────
PAD_PAGE  = "24px"
PAD_CARD  = "16px"
PAD_SHELL = "12px 24px"


# ══════════════════════════════════════════════════════════════
#  CHAT
# ══════════════════════════════════════════════════════════════

USER_BUBBLE = dict(
    background=SURFACE_ALT, border_radius="20px", padding="14px 20px",
    max_width="88%", margin_left="auto", width="fit-content",
)
ASSISTANT_ROW = dict(align="start", spacing="4", width="100%")

CHAT_STREAM = dict(
    width="100%", max_width=CHAT_MAX_W,
    margin="0 auto", padding="20px 24px", spacing="2",
)
CHAT_SHELL = dict(
    width="100%", max_width=CHAT_MAX_W,
    margin="0 auto", padding="12px 24px 20px", spacing="3",
)
STRUCTURE_HINT = dict(
    variant="soft", color=INK, background=SURFACE,
    width="100%", height="auto", padding="12px 16px",
)
COMPOSER = dict(
    background="white",
    border=f"1px solid {LINE}",
    border_radius="26px",
    box_shadow=SHADOW_SOFT,
    padding="12px 16px",
    width="100%",
)
COMPOSER_TEXTAREA = dict(
    placeholder="Обсудим книгу…",
    required=True,
    max_length=8000,
    variant="soft",
    background="transparent",
    border="0",
    box_shadow="none",
    outline="none",
    _focus={"box_shadow": "none", "outline": "none", "border": "0"},
    font_size="16px",
    line_height="1.5",
    min_height="80px",
    max_height="200px",
    padding="8px 0",
    resize="none",
    width="100%",
    vertical_align="top",
)
SEND_BTN = dict(
    border_radius=R_PILL,
    background=INK,
    color="white",
    size="2",
    flex_shrink="0",
)
DISCLAIMER = dict(size="1", color=MUTED, text_align="center", width="100%")


# ══════════════════════════════════════════════════════════════
#  SIDEBAR
# ══════════════════════════════════════════════════════════════

CHAT_LINK_BTN = dict(
    variant="ghost", color=INK, height="auto",
    background="transparent",
    _hover={"background": "transparent"},
    _active={"background": "transparent"},
    padding="12px", text_align="left", border_radius=R_SM, margin="0",
    flex="1", min_width="0",                 # ← вместо width="100%"
)
CHAT_LINK_STACK = dict(spacing="1", align="start", width="100%", min_width="0")
CHAT_LINK_TITLE = dict(
    size="2", weight="medium", width="100%",
    overflow="hidden", text_overflow="ellipsis", white_space="nowrap",
)
CHAT_LINK_BOOK = dict(
    size="1", color=MUTED, width="100%",
    overflow="hidden", text_overflow="ellipsis", white_space="nowrap",
)

SIDEBAR = dict(
    spacing="3", width=SIDEBAR_W, min_width=SIDEBAR_W, height=VIEWPORT_H,
    padding="20px 16px", background=SURFACE, border_right=f"1px solid {LINE}",
    position="relative", z_index="20", top="0", left="0",
)
SIDEBAR_HEADER = dict(width="100%", padding="8px 4px 20px")
NEW_CHAT_BTN = dict(
    variant="outline", color=INK, width="100%",
    justify_content="flex-start", size="3", border_color=BORDER_SOFT,
)
SIDEBAR_SECTION_LABEL = dict(
    size="1", color=MUTED, letter_spacing="0.1em", margin_top="24px",
)
SIDEBAR_LIST = dict(
    flex="1", overflow_y="auto", width="100%", min_height="0",
)
SIDEBAR_FOOTER = dict(
    spacing="2", padding_top="16px",
    border_top=f"1px solid {LINE}", width="100%",
)
STATUS_DOT = dict(width="7px", height="7px", border_radius="50%")

CHAT_ACTIONS = dict(
    variant="ghost", size="1", color=MUTED,
    flex_shrink="0", margin="0", width="28px", height="28px",
    border_radius=R_SM,
    _hover={"background": BORDER_SOFT, "color": INK},
    style={
        "@media (hover: hover) and (pointer: fine)": {"opacity": "0"},
        "&[data-state='open']": {"opacity": "1"},
    },
)
CHAT_ROW = dict(
    border_radius=R_SM, padding="2px 6px 2px 2px",
    spacing="1", width="100%", align="center", min_width="0",
    _hover={"background": HOVER_BG},
    style={
        "&:hover .chat-actions, &:focus-within .chat-actions, &[data-active='true'] .chat-actions": {"opacity": "1"},
    },
)
CHAT_MENU = dict(
    size="2", min_width="180px", background=BG,
    border=f"1px solid {LINE}", border_radius=R_MD,
)
DELETE_DIALOG = dict(
    max_width="420px", width="calc(100vw - 40px)",
    background=BG, border=f"1px solid {LINE}", border_radius=R_LG,
    padding=PAD_PAGE,
)


# ══════════════════════════════════════════════════════════════
#  STRUCTURE
# ══════════════════════════════════════════════════════════════

SECTION_BTN = dict(
    width="100%", justify_content="flex-start", color=INK,
    white_space="normal", height="auto", padding="10px",
    text_align="left", margin="0",
)

STRUCTURE_HEADER = dict(
    width="100%", flex_wrap="wrap", align="center", gap="12px",
)
STRUCTURE_GRID = dict(
    grid_template_columns="minmax(560px, 1fr) 310px",
    gap="0", width="100%", align_items="stretch",
    height="100%", min_height="0",
    style={
        "@media (max-width: 980px)": {
            "grid-template-columns": "1fr", "height": "auto",
        },
    },
)
STRUCTURE_SHELL = dict(
    width="100%", max_width="1440px", margin="0 auto",
    spacing="0", padding="0", height="100%", min_height="0",
)

TOC_COLUMN = dict(
    width="100%", spacing="1", padding="16px",
    background=SURFACE, border_radius=R_MD,
    position="sticky", top="16px",
    style={"@media (max-width: 820px)": {"position": "static"}},
)
TOC_LABEL = dict(size="1", letter_spacing="0.08em", color=MUTED)
TOC_SCROLL = dict(
    max_height="calc(100dvh - 190px)", overflow_y="auto",
    overscroll_behavior="contain", scrollbar_gutter="stable", width="100%",
)

EXCERPT_HEADING = dict(size="5")
EXCERPT_HINT = dict(size="1", color=MUTED)
EXCERPT = dict(
    padding="36px clamp(24px, 6vw, 64px)", background="white", border=f"1px solid {LINE}",
    border_radius=R_LG, width="100%", flex_shrink="0",
    box_shadow=SHADOW_SOFT,
)
EXCERPT_TEXT = dict(
    white_space="pre-wrap", line_height="1.85",
    font_family=SERIF, font_size="18px", max_width="720px", margin="0 auto",
)
EXCERPT_COLUMN = dict(spacing="4", width="100%", min_width="0")

# Markup editor layout.  The selectors make the legend on the right act as a
# visual filter without a state round-trip: hovering a row highlights all
# matching fragments on the paper.
MARKUP_CANVAS = dict(
    min_width="0", padding="44px clamp(22px, 5vw, 72px) 64px",
    background="#f0efeb", display="flex", justify_content="center",
    height="100%", overflow_y="auto", overscroll_behavior="contain",
    style={"@media (max-width: 980px)": {"height": "auto", "overflow-y": "visible"}},
)
MARKUP_PAPER = dict(
    width="100%", max_width="820px", min_height="760px", height="fit-content", flex_shrink="0",
    padding="54px clamp(40px, 7vw, 88px) 72px", background="#fffefb",
    border="1px solid #ebe8e1", border_radius="4px",
    box_shadow="0 12px 30px #2f281d0c",
)
MARKUP_SIDEBAR = dict(
    width="100%", min_width="0", padding="18px 16px 24px",
    background="#fbfaf7", border_left="1px solid #e8e5de",
    height="100%", max_height="100%", overflow_y="auto",
    overscroll_behavior="contain", scrollbar_gutter="stable",
    style={"@media (max-width: 980px)": {
        "height": "auto", "max-height": "none", "border-left": "0",
        "border-top": "1px solid #e8e5de",
    }},
)
MARKUP_PANEL_LABEL = dict(
    size="1", weight="bold", color="#403e39", letter_spacing="0.04em",
    text_transform="uppercase",
)
MARKUP_DIVIDER = dict(margin="4px 0 2px", border_color="#e6e3dc")
MARKUP_ISSUE_CARD = dict(
    width="100%", height="auto", padding="10px", text_align="left",
    justify_content="flex-start",
    border="1px solid", border_radius="7px", color=INK,
)
MARKUP_FRAGMENT = dict(
    position="relative", width="100%", padding="8px 12px 8px 44px",
    margin="0 0 4px", border="1px solid transparent",
    border_radius="7px", transition="background .16s ease, border-color .16s ease, box-shadow .16s ease",
    box_sizing="border-box",
)
MARKUP_ROOT_STYLE = {
    "& .fragment-epigraph:hover, & .fragment-epigraph:focus-within": {
        "background": "#fff7e8",
    },
    "& .fragment-attribution:hover, & .fragment-attribution:focus-within": {
        "background": "#eefafd",
    },
    "& .fragment-body:hover, & .fragment-body:focus-within": {
        "background": "#eefaf5",
    },
    "& .fragment-opening:hover, & .fragment-opening:focus-within": {
        "background": "#f6f1ff",
    },
    "& .block-selector": {
        "position": "absolute",
        "left": "10px",
        "top": "9px",
        "width": "22px",
        "height": "22px",
        "min_width": "22px",
        "padding": "0",
        "border_radius": "9999px",
        "border": "1.5px solid #b9b3a8",
        "background": "#fffefb",
        "color": "white",
        "display": "flex",
        "align_items": "center",
        "justify_content": "center",
        "box_sizing": "border-box",
        "appearance": "none",
        "cursor": "pointer",
        "opacity": "0",
        "transform": "scale(.92)",
        "transition": "opacity .15s ease, transform .15s ease, background .15s ease, border-color .15s ease",
    },
    "& .markup-fragment:hover .block-selector, & .block-selector:focus-visible": {
        "opacity": "1",
        "transform": "scale(1)",
    },
    "& .fragment-epigraph .block-selector:hover": {
        "border_color": "#ed8500",
    },
    "& .fragment-attribution .block-selector:hover": {
        "border_color": "#1698b7",
    },
    "& .fragment-body .block-selector:hover": {
        "border_color": "#0ca66f",
    },
    "& .fragment-opening .block-selector:hover": {
        "border_color": "#8357e8",
    },
    "& .markup-fragment[data-selected='true'] .block-selector": {
        "opacity": "1",
        "transform": "scale(1)",
    },
    "& .fragment-epigraph[data-selected='true']": {
        "background": "#fff2cf", "border_color": "#ed8500",
    },
    "& .fragment-epigraph[data-selected='true'] .block-selector": {
        "background": "#ed8500", "border_color": "#ed8500",
    },
    "& .fragment-attribution[data-selected='true']": {
        "background": "#e6f8fc", "border_color": "#1698b7",
    },
    "& .fragment-attribution[data-selected='true'] .block-selector": {
        "background": "#1698b7", "border_color": "#1698b7",
    },
    "& .fragment-body[data-selected='true']": {
        "background": "#e5f8ef", "border_color": "#0ca66f",
    },
    "& .fragment-body[data-selected='true'] .block-selector": {
        "background": "#0ca66f", "border_color": "#0ca66f",
    },
    "& .fragment-opening[data-selected='true']": {
        "background": "#f1eaff", "border_color": "#8357e8",
    },
    "& .fragment-opening[data-selected='true'] .block-selector": {
        "background": "#8357e8", "border_color": "#8357e8",
    },
    "@media (hover: none), (pointer: coarse)": {
        "& .block-selector": {
            "opacity": ".55",
            "transform": "scale(1)",
        },
    },
    "& .fragment-heading.review-target": {
        "background": "#eaf3ff", "outline": "2px solid #478ce7",
    },
    "& .fragment-epigraph.review-target": {
        "background": "#fff2cf", "outline": "2px solid #ed8500",
    },
    "& .fragment-body.review-target": {
        "background": "#e5f8ef", "outline": "2px solid #0ca66f",
    },
    "& .fragment-opening.review-target": {
        "background": "#f1eaff", "outline": "2px solid #8357e8",
    },
    "& .fragment-attribution.review-target": {
        "background": "#e6f8fc", "outline": "2px solid #1698b7",
    },
}

PAGE_TITLE = dict(size="6", font_family=SERIF, font_weight="400")




# ══════════════════════════════════════════════════════════════
#  WELCOME
# ══════════════════════════════════════════════════════════════

WELCOME_CENTER = dict(width="100%", flex="1", overflow_y="auto")
WELCOME_SHELL = dict(
    width="100%", max_width="720px",
    spacing="5", padding="24px", align="center",
)
WELCOME_HERO = dict(spacing="3", align="center")
WELCOME_TITLE = dict(
    size="8", font_family=SERIF, font_weight="400", text_align="center",
)
WELCOME_SUBTITLE = dict(color=MUTED, text_align="center")

STUB_COMPOSER = dict(
    padding="16px", background="white", border=f"1px solid {LINE}",
    border_radius=R_XL, width="100%", spacing="3",
    box_shadow=SHADOW_SOFT,
)
STUB_TEXTAREA = dict(
    disabled=True, width="100%", min_height="80px",
    background="transparent", variant="soft", box_shadow="none",
)

POPOVER_CONTENT = dict(width="min(400px, 90vw)", side="top", align="start")
POPOVER_LIST = dict(max_height="260px", overflow_y="auto", width="100%")
UPLOAD_ZONE = dict(
    padding="18px", border=f"1px dashed {LINE}",
    border_radius=R_SM, width="100%",
)
BOOK_CHOICE = dict(
    variant="outline", color=INK, width="100%", height="auto",
    min_height="70px", padding="20px", border_color=LINE,
    border_radius=R_LG, background="white", justify_content="flex-start",
)
