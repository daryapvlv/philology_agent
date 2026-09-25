"""python -m ingest book.epub — полный приём и импорт книги в Neo4j."""
import argparse
from pathlib import Path

from structure.graph_db.repository import get_repository
from ingest import save_upload, process_upload


def main():
    parser = argparse.ArgumentParser(description='Загрузить EPUB/Markdown в библиотеку Neo4j')
    parser.add_argument('book', type=Path)
    args = parser.parse_args()
    get_repository()  # Проверяем подключение до парсинга и оплачиваемого анализа.
    book_id = save_upload(args.book.name, args.book.read_bytes())
    process_upload(book_id)
    print(book_id)


if __name__ == '__main__':
    main()
