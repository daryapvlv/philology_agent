"""Первичный импорт полностью подготовленной книги в Neo4j."""
from copy import deepcopy
from uuid import uuid4

from infrastructure.book_artifacts import save_elements
from infrastructure.files import json_sha256
from structure.graph_db.repository import pack, write_scene_layer, write_sections
from structure.rules.snapshot import validate_snapshot


class IngestRepository:
    """Записывает новый снимок, не перезаписывая уже импортированную книгу."""

    def __init__(self, books):
        self.books = books

    def import_book(self, result, elements):
        book_id = result["document_id"]
        if not elements or not all(isinstance(element.get("text"), str) for element in elements):
            raise ValueError("Нужен непустой список текстовых элементов")
        section_ids = [section["id"] for section in result["sections"]]
        if len(section_ids) != len(set(section_ids)) or any(
            section.get("parent_id") not in [None, *section_ids]
            for section in result["sections"]
        ):
            raise ValueError("Некорректные ID или родители разделов")

        text = "\n".join(element["text"] for element in elements)
        validate_snapshot(result, elements)
        # elements.json — артефакт импорта, а не часть графовой модели.
        save_elements(book_id, elements)
        metadata = deepcopy({
            key: value for key, value in result.items() if key not in {"sections", "scenes"}
        })
        metadata.get("source", {}).pop("elements_path", None)
        text_hash = json_sha256([element["text"] for element in elements])
        token = uuid4().hex

        def create(tx):
            row = tx.run(
                """MERGE (b:LiteraryBook {id:$id})
                ON CREATE SET b.revision=0, b.import_token=$token, b.text_hash=$hash
                RETURN b.import_token AS token, b.text_hash AS hash, b.schema_version AS schema""",
                id=book_id,
                token=token,
                hash=text_hash,
            ).single()
            if row["schema"] is not None:
                if row["hash"] != text_hash:
                    raise ValueError("Книга уже импортирована с другой версией текста")
                return book_id

            tx.run(
                """MATCH (b:LiteraryBook {id:$id})
                SET b.title=$title, b.text=$text, b.metadata=$metadata, b.original_sections=$original,
                    b.total_elements=$count, b.schema_version=2""",
                id=book_id,
                title=result.get("book_title") or book_id[:12],
                text=text,
                metadata=pack(metadata),
                original=pack(result["sections"]),
                count=len(elements),
            ).consume()

            write_sections(tx, book_id, result["sections"], elements,
                           result.get("blocks", []))
            for chapter_id, layer in result.get("scenes", {}).items():
                write_scene_layer(tx, book_id, chapter_id, layer)
            return book_id

        with self.books.driver.session(database=self.books.database) as session:
            return session.execute_write(create)
