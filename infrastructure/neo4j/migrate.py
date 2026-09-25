"""Отдельная от runtime подготовка Neo4j-схемы приложения."""
from infrastructure.book_artifacts import elements_path, load_elements, save_elements
from structure.graph_db.repository import get_repository
from structure.graph_db.repository import element_offsets, write_sections


RETRIEVAL_SCHEMA = (
    'CREATE CONSTRAINT retrieval_chunk_id IF NOT EXISTS '
    'FOR (n:TextChunk) REQUIRE (n.book_id,n.id) IS UNIQUE',
    "CREATE FULLTEXT INDEX retrieval_text IF NOT EXISTS FOR (n:TextChunk) ON EACH [n.text] "
    "OPTIONS {indexConfig: {`fulltext.analyzer`: 'russian'}}",
    "CREATE FULLTEXT INDEX retrieval_scenes IF NOT EXISTS FOR (n:NarrativeScene) "
    "ON EACH [n.title,n.summary] OPTIONS {indexConfig: {`fulltext.analyzer`: 'russian'}}",
    "CREATE FULLTEXT INDEX retrieval_context IF NOT EXISTS FOR (n:TextChunk) "
    "ON EACH [n.narrative_context] OPTIONS {indexConfig: {`fulltext.analyzer`: 'russian'}}",
    'CREATE CONSTRAINT entity_mention_id IF NOT EXISTS '
    'FOR (n:EntityMention) REQUIRE (n.book_id,n.id) IS UNIQUE',
    'CREATE CONSTRAINT entity_reference_id IF NOT EXISTS '
    'FOR (n:EntityReference) REQUIRE (n.book_id,n.id) IS UNIQUE',
    'CREATE CONSTRAINT chapter_entity_id IF NOT EXISTS '
    'FOR (n:ChapterEntity) REQUIRE (n.book_id,n.id) IS UNIQUE',
    'CREATE CONSTRAINT book_entity_id IF NOT EXISTS '
    'FOR (n:BookEntity) REQUIRE (n.book_id,n.id) IS UNIQUE',
    'CREATE CONSTRAINT entity_id IF NOT EXISTS '
    'FOR (n:Entity) REQUIRE (n.book_id,n.id) IS UNIQUE',
    'CREATE CONSTRAINT narrative_event_id IF NOT EXISTS '
    'FOR (n:NarrativeEvent) REQUIRE (n.book_id,n.id) IS UNIQUE',
)


def migrate_text_elements(books):
    """Вынести старые TextElement в artifacts и заменить их координатами секций.

    Файл всегда записывается раньше удаления узлов. Повторный запуск безопасен.
    """
    with books.driver.session(database=books.database) as session:
        book_ids = [row['id'] for row in session.run(
            'MATCH (b:LiteraryBook) RETURN b.id AS id ORDER BY b.id'
        )]
        for book_id in book_ids:
            path = elements_path(book_id)
            if not path.exists():
                rows = session.run('''MATCH (e:TextElement {book_id:$id})
                    RETURN properties(e) AS element ORDER BY e.position''', id=book_id).data()
                if not rows:
                    raise ValueError(
                        f'Для книги {book_id} нет ни elements.json, ни TextElement'
                    )
                elements = []
                for row in rows:
                    source = row['element']
                    element = {'text': source['text']}
                    if source.get('type'):
                        element['type'] = source['type']
                    elements.append(element)
                save_elements(book_id, elements)

            elements = load_elements(book_id)
            starts, ends = element_offsets(elements)
            sections = session.run('''MATCH (s:BookSection {book_id:$id})
                RETURN s.id AS id, s.start AS start, s.end AS end,
                       s.title_position AS title_position''', id=book_id).data()
            updates = []
            for section in sections:
                start, end = section['start'], section['end']
                updates.append({
                    'id': section['id'],
                    'start_char': starts[start],
                    'end_char': ends[end - 1] if end is not None else None,
                    'title_position_char': (
                        starts[section['title_position']]
                        if section['title_position'] is not None else None
                    ),
                })
            with session.begin_transaction() as tx:
                tx.run('''UNWIND $rows AS row
                    MATCH (s:BookSection {book_id:$id,id:row.id})
                    SET s.start_char=row.start_char, s.end_char=row.end_char,
                        s.title_position_char=row.title_position_char''',
                    id=book_id, rows=updates).consume()
                tx.run(
                    'MATCH (e:TextElement {book_id:$id}) DETACH DELETE e', id=book_id,
                ).consume()
                tx.commit()
        session.run('DROP CONSTRAINT text_element_id IF EXISTS').consume()


def migrate_section_blocks(books):
    """Материализовать содержимое разделов и переподключить сцены к body."""
    with books.driver.session(database=books.database) as session:
        book_ids = [row['id'] for row in session.run(
            'MATCH (b:LiteraryBook) RETURN b.id AS id ORDER BY b.id'
        )]
    for book_id in book_ids:
        result = books.load_structure(book_id)
        elements = load_elements(book_id)
        with books.driver.session(database=books.database) as session:
            def update(tx):
                write_sections(tx, book_id, result['sections'], elements,
                               result.get('blocks', []))
                tx.run('MATCH (b:LiteraryBook {id:$id}) SET b.schema_version=2',
                       id=book_id).consume()
            session.execute_write(update)


def migrate(repository=None):
    # get_repository() также применяет существующую structure-схему.
    books = repository or get_repository()
    with books.driver.session(database=books.database) as session:
        for statement in RETRIEVAL_SCHEMA:
            session.run(statement).consume()
        for name in ('retrieval_text', 'retrieval_scenes', 'retrieval_context'):
            session.run('CALL db.awaitIndex($name, 60)', name=name).consume()
    migrate_text_elements(books)
    migrate_section_blocks(books)


def main():
    migrate()
    print('Neo4j schema is ready')


if __name__ == '__main__':
    main()
