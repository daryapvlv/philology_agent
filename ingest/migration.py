"""Импорт сохранённого локального снимка книги в Neo4j.

Это переходный инструмент для старых артефактов. Новый рабочий поток пишет
данные в graph_db напрямую.
"""
from pathlib import Path

from infrastructure.files import file_sha256, read_json
from structure.graph_db.repository import get_repository

from .repository import IngestRepository


def import_saved_book(directory, repository=None):
    directory = Path(directory)
    result = read_json(directory / "structure.json")
    elements_path = directory / "elements.json"
    if result.get("source", {}).get("sha256") != file_sha256(elements_path):
        raise ValueError("Структура относится к другой версии elements.json")
    books = repository or get_repository()
    return IngestRepository(books).import_book(result, read_json(elements_path))
