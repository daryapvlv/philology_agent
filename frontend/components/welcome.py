"""Стартовый экран: приветствие и доступное поле сообщения."""
import reflex as rx

from ..state import State
from ..styles import ACCENT, WELCOME_CENTER, WELCOME_SHELL, WELCOME_HERO, WELCOME_TITLE, WELCOME_SUBTITLE
from .chat import _composer
from .book_picker import _status_block


def _hero():
    return rx.vstack(
        rx.icon("book-open", size=36, color=ACCENT),
        rx.heading("Что обсудим?", **WELCOME_TITLE),
        rx.text("Задай вопрос или добавь книгу — сейчас или позже.", **WELCOME_SUBTITLE),
        **WELCOME_HERO,
    )


def welcome():
    return rx.center(
        rx.cond(
            State.sidebar_hidden,
            rx.icon_button(
                "panel-left-open",
                variant="ghost",
                on_click=State.toggle_sidebar,
                aria_label="Показать список чатов",
                position="absolute",
                top="10px",
                left="18px",
            ),
        ),
        rx.vstack(
            _hero(),
            _composer(),
            rx.cond(State.upload_status != "", _status_block()),
            **WELCOME_SHELL,
        ),
        position="relative",
        **WELCOME_CENTER,
    )
