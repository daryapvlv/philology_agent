"""Неизменяемые артефакты разбора исходных книг."""
from pathlib import Path

from .files import json_sha256, read_json, write_json


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOOKS_DIR = PROJECT_ROOT / "artifacts" / "books"


def book_dir(document_id: str) -> Path:
    return BOOKS_DIR / document_id


def elements_path(document_id: str) -> Path:
    return book_dir(document_id) / "elements.json"


def save_elements(document_id: str, elements: list[dict]) -> Path:
    """Сохранить результат Unstructured, не подменяя уже импортированный текст."""
    path = elements_path(document_id)
    if path.exists():
        current = read_json(path)
        if json_sha256(current) != json_sha256(elements):
            raise ValueError("Артефакт elements.json уже содержит другую версию текста")
        return path
    write_json(path, elements)
    return path


def load_elements(document_id: str) -> list[dict]:
    path = elements_path(document_id)
    if not path.exists():
        raise ValueError(
            f"Не найден артефакт разбора книги: {path}. Запусти миграцию Neo4j."
        )
    elements = read_json(path)
    if not isinstance(elements, list) or not elements:
        raise ValueError(f"Некорректный артефакт разбора книги: {path}")
    return elements
