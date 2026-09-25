"""Neo4j — рабочее хранилище структуры книг.

Канонический текст, разделы, слои и сцены находятся в графе. Результат разбора
на текстовые элементы хранится отдельным неизменяемым артефактом и загружается
только для алгоритмов, которым всё ещё нужны номера исходных элементов.
"""
from functools import lru_cache
import json
import os

from neo4j import GraphDatabase

from infrastructure.book_artifacts import load_elements
from ..chunks import scene_chunks
from ..rules.snapshot import validate_snapshot


class ConflictError(ValueError):
    """Пользователь или другой агент уже изменил книгу."""


def pack(value):
    return json.dumps(value, ensure_ascii=False)


def scalar_properties(value):
    return {k: v for k, v in value.items() if v is None or isinstance(v, (str, int, float, bool))}


def graph_properties(value):
    """Скалярные свойства плюс числовые векторы для Neo4j."""
    props = scalar_properties(value)
    for key, item in value.items():
        if isinstance(item, list) and all(isinstance(x, (int, float)) for x in item):
            props[key] = item
    return props


def element_offsets(elements):
    """Границы элементов в каноническом ``\n``-разделённом тексте."""
    starts, ends, offset = [], [], 0
    for element in elements:
        starts.append(offset)
        offset += len(element['text'])
        ends.append(offset)
        offset += 1
    return starts, ends


def section_properties(section, starts, ends):
    """Свойства узла раздела с независимыми от элемента координатами."""
    row = scalar_properties(section)
    start, end = section['start'], section['end']
    row['start_char'] = starts[start]
    row['end_char'] = ends[end - 1] if end is not None else None
    title_position = section.get('title_position')
    row['title_position_char'] = starts[title_position] if title_position is not None else None
    body_start, body_end = section.get('body_start'), section.get('body_end')
    row['body_start_char'] = starts[body_start] if body_start is not None and body_start < len(starts) else None
    row['body_end_char'] = ends[body_end - 1] if body_end is not None and body_end > 0 else None
    return row


def _element_range(elements, start, end):
    return "\n".join(element["text"] for element in elements[start:end])


def content_block_rows(book_id, sections, blocks, elements):
    """Материализовать содержимое смысловых разделов для Neo4j.

    Body хранит текст без выделенных примечаний и исходный диапазон книги.
    Примечания и эпиграфы остаются отдельными узлами.
    """
    starts, ends = element_offsets(elements)
    children = {section.get("parent_id") for section in sections}
    by_id = {section["id"]: section for section in sections}
    rows = []
    for section in sections:
        start = section.get("body_start")
        end = section.get("body_end")
        if start is None:
            start = section["start"] + (section.get("title_position") == section["start"])
        if end is None:
            end = section.get("end")
        is_pure_container = (
            section["id"] in children
            and section.get("role") in {"collection", "work", "volume", "part", "act"}
        )
        # У свежей разметки body_end контейнера ограничен первым ребёнком: это
        # собственный пролог, а не дубликат текста глав. Старые снимки, где body
        # совпадает со всем диапазоном контейнера, по-прежнему не материализуем.
        duplicates_children = is_pure_container and end == section.get("end")
        if duplicates_children or type(start) is not int or type(end) is not int:
            continue
        if not 0 <= start < end <= len(elements):
            continue
        excluded = sorted(
            (max(start, block['start']), min(end, block['end']))
            for block in blocks
            if block.get('section_id') == section['id']
            and block.get('role') in {'footnotes', 'epigraph'}
            and block['start'] < end and block['end'] > start
        )
        excluded_positions = {
            position for left, right in excluded for position in range(left, right)
        }
        body_text = "\n".join(
            elements[position]['text'] for position in range(start, end)
            if position not in excluded_positions
        )
        if body_text.strip():
            rows.append({
                "id": f'{section["id"]}:block:body',
                "book_id": book_id, "section_id": section["id"],
                "role": "body", "start": start, "end": end,
                "start_char": starts[start], "end_char": ends[end - 1],
                "text": body_text,
                "excluded_note_ids": [block['id'] for block in blocks
                                      if block.get('section_id') == section['id']
                                      and block.get('role') == 'footnotes'
                                      and block['start'] < end and block['end'] > start],
            })

    for block in blocks:
        section = by_id.get(block.get("section_id"))
        start, end = block.get("start"), block.get("end")
        if section is None or block.get("role") not in {"epigraph", "footnotes"}:
            continue
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(elements):
            continue
        row = {
            "id": block["id"], "book_id": book_id, "section_id": section["id"],
            "role": block["role"],
            "start": start, "end": end, "start_char": starts[start],
            "end_char": ends[end - 1], "source": block.get("source", "system"),
        }
        attribution = block.get("attribution_start")
        if block["role"] == "epigraph" and type(attribution) is int and start <= attribution < end:
            row["text"] = _element_range(elements, start, attribution)
            row["attribution"] = _element_range(elements, attribution, end)
            row["attribution_start"] = attribution
            row["attribution_start_char"] = starts[attribution]
        else:
            row["text"] = _element_range(elements, start, end)
            if block["role"] == "epigraph":
                row["attribution"] = None
        rows.append(row)
    return sorted(rows, key=lambda row: (row["section_id"], row["start"], row["end"]))


def write_content_blocks(tx, book_id, sections, blocks, elements):
    """Синхронизировать эпиграф, body и примечания как узлы содержимого."""
    rows = content_block_rows(book_id, sections, blocks, elements)
    ids = [row["id"] for row in rows]
    tx.run(
        'MATCH (n:SectionBlock {book_id:$id}) WHERE NOT n.id IN $ids DETACH DELETE n',
        id=book_id, ids=ids,
    ).consume()
    tx.run('''UNWIND $rows AS row
        MERGE (n:SectionBlock {book_id:$id,id:row.id}) SET n=row
        WITH n,row MATCH (s:BookSection {book_id:$id,id:row.section_id})
        MERGE (s)-[:HAS_BLOCK]->(n)''', id=book_id, rows=rows).consume()
    tx.run(
        'MATCH (:SectionBlock {book_id:$id})-[r:NEXT_BLOCK]->() DELETE r', id=book_id,
    ).consume()
    pairs = []
    for left, right in zip(rows, rows[1:]):
        if left["section_id"] == right["section_id"]:
            pairs.append([left["id"], right["id"]])
    tx.run('''UNWIND $pairs AS pair
        MATCH (a:SectionBlock {book_id:$id,id:pair[0]}),
              (b:SectionBlock {book_id:$id,id:pair[1]})
        MERGE (a)-[:NEXT_BLOCK]->(b)''', id=book_id, pairs=pairs).consume()
    # Переход со старой схемы выполняется безопасно при любой следующей записи.
    tx.run('''MATCH (:LiteraryBook {id:$id})-[r:HAS_LAYER]->(:SceneLayer) DELETE r''',
           id=book_id).consume()
    tx.run('''MATCH (:BookSection {book_id:$id})-[r:HAS_SCENE_LAYER]->(:SceneLayer)
        DELETE r''', id=book_id).consume()
    tx.run('''MATCH (body:SectionBlock {book_id:$id,role:'body'}),
                   (l:SceneLayer {book_id:$id})
        WHERE l.chapter_id=body.section_id
        MERGE (body)-[:HAS_SCENE_LAYER]->(l)''', id=book_id).consume()
    tx.run('''MATCH (body:SectionBlock {book_id:$id,role:'body'}),
                   (scene:NarrativeScene {book_id:$id})
        WHERE scene.chapter_id=body.section_id
        MERGE (body)-[:HAS_SCENE]->(scene)''', id=book_id).consume()


def write_sections(tx, book_id, sections, elements, blocks=()):
    """Записать канонические секции внутри уже открытой транзакции."""
    ids = [section['id'] for section in sections]
    starts, ends = element_offsets(elements)
    tx.run(
        'MATCH (s:BookSection {book_id:$id}) WHERE NOT s.id IN $ids DETACH DELETE s',
        id=book_id,
        ids=ids,
    ).consume()
    tx.run(
        'MATCH (:LiteraryBook {id:$id})-[r:HAS_SECTION]->(:BookSection) DELETE r',
        id=book_id,
    ).consume()
    tx.run('''UNWIND $rows AS row
        MERGE (s:BookSection {book_id:$id, id:row.id}) SET s = row, s.book_id=$id
        WITH s,row WHERE row.parent_id IS NULL
        MATCH (b:LiteraryBook {id:$id}) MERGE (b)-[:HAS_SECTION]->(s)''',
        id=book_id, rows=[section_properties(section, starts, ends) for section in sections]).consume()
    tx.run(
        'MATCH (:BookSection {book_id:$id})-[r:CONTAINS]->() DELETE r',
        id=book_id,
    ).consume()
    tx.run('''UNWIND $rows AS row
        MATCH (s:BookSection {book_id:$id, id:row.id})
        MATCH (p:BookSection {book_id:$id, id:row.parent_id}) MERGE (p)-[:CONTAINS]->(s)''',
        id=book_id, rows=sections).consume()
    write_content_blocks(tx, book_id, sections, blocks, elements)


def rebuild_next_scene_chain(tx, book_id):
    """Пересобрать порядок чтения сцен по всей книге детерминированно, без LLM.

    Сцены глав со статусом ready/partial связываются `NEXT_SCENE` по позиции в
    каноническом тексте, включая переходы между главами. Вызывается при каждом
    сохранении слоя сцен, поэтому межглавная навигация доступна сразу после
    построения соседних глав и не требует отдельного дорогого entity-resolution
    шага (тот материализует Graph v1 сущностей, а не порядок чтения).
    """
    tx.run('''MATCH (:NarrativeScene {book_id:$id})-[r:NEXT_SCENE]->()
        DELETE r''', id=book_id).consume()
    scene_rows = tx.run('''MATCH (l:SceneLayer {book_id:$id})-[:HAS_SCENE]->
            (s:NarrativeScene {book_id:$id})
        WHERE l.status IN ['ready','partial']
        RETURN DISTINCT s.id AS id, s.start_char AS start_char, s.end_char AS end_char
        ORDER BY start_char, end_char, id''', id=book_id).data()
    ids = [row['id'] for row in scene_rows]
    pairs = [[left, right] for left, right in zip(ids, ids[1:])]
    tx.run('''UNWIND $pairs AS pair
        MATCH (a:NarrativeScene {book_id:$id, id:pair[0]}), (b:NarrativeScene {book_id:$id, id:pair[1]})
        MERGE (a)-[:NEXT_SCENE]->(b)''', id=book_id, pairs=pairs).consume()
    return len(pairs)


def rebuild_next_chunk_chain(tx, book_id):
    """Пересобрать глобальный порядок TextChunk по порядку чтения."""
    tx.run('''MATCH (:TextChunk {book_id:$id})-[r:NEXT_CHUNK]->(:TextChunk)
        DELETE r''', id=book_id).consume()
    rows = tx.run('''MATCH (c:TextChunk {book_id:$id})
        RETURN c.id AS id ORDER BY c.start_char,c.end_char,c.id''', id=book_id).data()
    pairs = [[left['id'], right['id']] for left, right in zip(rows, rows[1:])]
    tx.run('''UNWIND $pairs AS pair
        MATCH (a:TextChunk {book_id:$id,id:pair[0]}),
              (b:TextChunk {book_id:$id,id:pair[1]})
        MERGE (a)-[:NEXT_CHUNK]->(b)''', id=book_id, pairs=pairs).consume()
    return len(pairs)


def write_scene_layer(tx, book_id, chapter_id, layer, old=None):
    """Записать канонический слой сцен внутри уже открытой транзакции."""
    metadata = {key: value for key, value in layer.items() if key != 'items'}
    props = scalar_properties(metadata)
    props.update(book_id=book_id, chapter_id=chapter_id, metadata=pack(metadata))
    tx.run('''MERGE (l:SceneLayer {book_id:$id, chapter_id:$chapter}) SET l=$props''',
        id=book_id, chapter=chapter_id, props=props).consume()
    tx.run('''MATCH (body:SectionBlock {book_id:$id,section_id:$chapter,role:'body'}),
        (l:SceneLayer {book_id:$id,chapter_id:$chapter})
        MERGE (body)-[:HAS_SCENE_LAYER]->(l)''',
        id=book_id, chapter=chapter_id).consume()
    items = layer['items']
    current = {scene['id']: scene for scene in items}
    invalid_mentions = [
        scene['id'] for scene in (old or {}).get('items', [])
        if layer.get('status') != 'ready'
        or scene['id'] not in current
        or any(scene.get(key) != current[scene['id']].get(key)
               for key in ('start_char', 'end_char', 'text_hash'))
    ]
    if invalid_mentions:
        tx.run('''MATCH (c:ChapterEntity {book_id:$id,chapter_id:$chapter})
            DETACH DELETE c''', id=book_id, chapter=chapter_id).consume()
        tx.run('''MATCH (chapter:BookSection {book_id:$id,id:$chapter})
            REMOVE chapter.chapter_entity_status,chapter.chapter_entity_mentions''',
            id=book_id, chapter=chapter_id).consume()
        tx.run('''MATCH (e:BookEntity {book_id:$id})
            WHERE NOT (:ChapterEntity {book_id:$id})-[:RESOLVES_TO]->(e)
            DETACH DELETE e''', id=book_id).consume()
        tx.run('''MATCH (r:EntityReference {book_id:$id})
            WHERE r.scene_id IN $scenes DETACH DELETE r''',
            id=book_id, scenes=invalid_mentions).consume()
        tx.run('''MATCH (m:EntityMention {book_id:$id})
            WHERE m.scene_id IN $scenes DETACH DELETE m''',
            id=book_id, scenes=invalid_mentions).consume()
        # Инвариант 3: смена hash/статуса сцены удаляет и её нарративную разметку —
        # события узлами (NarrativeEvent) и контекст чанков (TextChunk.narrative_*),
        # не только устаревший флаг на самой сцене (тот уже теряет силу сам, когда
        # narrative_hash перестаёт совпадать с новым text_hash после SET s=row).
        tx.run('''MATCH (:NarrativeScene {book_id:$id})-[:HAS_EVENT]->
                (e:NarrativeEvent {book_id:$id})
            WHERE e.scene_id IN $scenes DETACH DELETE e''',
            id=book_id, scenes=invalid_mentions).consume()
        tx.run('''MATCH (s:NarrativeScene {book_id:$id})-[r]-(:NarrativeScene {book_id:$id})
            WHERE s.id IN $scenes AND type(r) = 'SCENE_LINK' DELETE r''',
            id=book_id, scenes=invalid_mentions).consume()
        tx.run('''MATCH (s:NarrativeScene {book_id:$id})-[:HAS_CHUNK]->
                (c:TextChunk {book_id:$id})
            WHERE s.id IN $scenes
            REMOVE c.narrative_context, c.narrative_representation,
                   c.narrative_speaker_entity_id, c.narrative_level,
                   c.narrative_embedded_kind''',
            id=book_id, scenes=invalid_mentions).consume()
    tx.run('''MATCH (s:NarrativeScene {book_id:$id, chapter_id:$chapter})
        WHERE NOT s.id IN $ids DETACH DELETE s''',
        id=book_id, chapter=chapter_id, ids=[scene['id'] for scene in items]).consume()
    tx.run('''UNWIND $rows AS row
        MERGE (s:NarrativeScene {book_id:$id, id:row.id}) SET s=row, s.book_id=$id
        WITH s MATCH (l:SceneLayer {book_id:$id, chapter_id:$chapter}) MERGE (l)-[:HAS_SCENE]->(s)''',
        id=book_id, chapter=chapter_id,
        rows=[graph_properties(scene) for scene in items]).consume()
    tx.run('''MATCH (:SectionBlock {book_id:$id,section_id:$chapter,role:'body'})
        -[r:HAS_SCENE]->(:NarrativeScene) DELETE r''',
        id=book_id, chapter=chapter_id).consume()
    tx.run('''UNWIND $rows AS row
        MATCH (body:SectionBlock {book_id:$id,section_id:$chapter,role:'body'}),
              (scene:NarrativeScene {book_id:$id,id:row.id})
        MERGE (body)-[:HAS_SCENE]->(scene)''',
        id=book_id, chapter=chapter_id, rows=items).consume()
    book = tx.run('MATCH (b:LiteraryBook {id:$id}) RETURN b.text AS text',
                  id=book_id).single()
    blocks = tx.run('''MATCH (section:BookSection {book_id:$id,id:$chapter})
            -[:HAS_BLOCK]->(body:SectionBlock {role:'body'})
        OPTIONAL MATCH (section)-[:HAS_BLOCK]->(excluded:SectionBlock)
        WHERE excluded.role IN ['epigraph','footnotes']
        RETURN body.start_char AS start,body.end_char AS end,
               collect({start:excluded.start_char,end:excluded.end_char}) AS excluded''',
        id=book_id, chapter=chapter_id).single()
    body_ranges = []
    if blocks is not None:
        cursor = blocks['start']
        excluded = sorted((span for span in blocks['excluded']
                           if type(span.get('start')) is int and type(span.get('end')) is int),
                          key=lambda span: (span['start'], span['end']))
        for span in excluded:
            if cursor < span['start']:
                body_ranges.append(dict(start=cursor, end=min(span['start'], blocks['end'])))
            cursor = max(cursor, span['end'])
        if cursor < blocks['end']:
            body_ranges.append(dict(start=cursor, end=blocks['end']))
    active = items if layer.get('status') in {'ready', 'partial'} else []
    chunks = [row for scene in active
              for row in scene_chunks(book['text'], scene, ranges=body_ranges)]
    tx.run('''MATCH (c:TextChunk {book_id:$id,chapter_id:$chapter})
        WHERE NOT c.id IN $ids DETACH DELETE c''', id=book_id, chapter=chapter_id,
        ids=[row['id'] for row in chunks]).consume()
    tx.run('''UNWIND $rows AS row
        MERGE (c:TextChunk {book_id:$id,id:row.id}) SET c += row, c.book_id=$id
        WITH c,row
        MATCH (b:LiteraryBook {id:$id}),
              (s:NarrativeScene {book_id:$id,id:row.scene_id})
        MERGE (b)-[:HAS_CHUNK]->(c)
        MERGE (s)-[:HAS_CHUNK]->(c)''', id=book_id, rows=chunks).consume()
    # Любое изменение Scene/Chunk требует явной финализации поискового индекса.
    tx.run('''MATCH (b:LiteraryBook {id:$id})
        REMOVE b.search_generation,b.search_text_hash,b.search_embedding_model,
               b.search_chunk_count,b.search_embedded_count,b.search_dimensions,
               b.search_scene_count,b.search_scene_embedded_count''', id=book_id).consume()
    rebuild_next_scene_chain(tx, book_id)
    rebuild_next_chunk_chain(tx, book_id)


class BookRepository:
    def __init__(self, driver, database='neo4j'):
        self.driver, self.database = driver, database

    def close(self):
        self.driver.close()

    def _read(self, callback, *args):
        with self.driver.session(database=self.database) as session:
            return session.execute_read(callback, *args)

    def _write(self, callback, *args):
        with self.driver.session(database=self.database) as session:
            return session.execute_write(callback, *args)

    def setup(self):
        self.driver.verify_connectivity()
        constraints = [
            ('literary_book_id', 'LiteraryBook', 'n.id'),
            ('book_section_id', 'BookSection', '(n.book_id, n.id)'),
            ('section_block_id', 'SectionBlock', '(n.book_id, n.id)'),
            ('scene_layer_id', 'SceneLayer', '(n.book_id, n.chapter_id)'),
            ('narrative_scene_id', 'NarrativeScene', '(n.book_id, n.id)'),
            ('entity_mention_id', 'EntityMention', '(n.book_id, n.id)'),
            ('entity_reference_id', 'EntityReference', '(n.book_id, n.id)'),
            ('chapter_entity_id', 'ChapterEntity', '(n.book_id, n.id)'),
            ('book_entity_id', 'BookEntity', '(n.book_id, n.id)'),
            ('entity_id', 'Entity', '(n.book_id, n.id)'),
        ]
        with self.driver.session(database=self.database) as session:
            for name, label, properties in constraints:
                session.run(f'CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{label}) REQUIRE {properties} IS UNIQUE').consume()

    def catalog(self):
        return self._read(lambda tx: tx.run(
            'MATCH (b:LiteraryBook) RETURN b.id AS id, b.title AS title ORDER BY b.title, b.id'
        ).data())

    def exists(self, book_id):
        return self._read(lambda tx: tx.run(
            'MATCH (b:LiteraryBook {id:$id}) RETURN b.id', id=book_id,
        ).single() is not None)

    @staticmethod
    def _load(tx, book_id):
        row = tx.run('MATCH (b:LiteraryBook {id:$id}) RETURN b', id=book_id).single()
        if row is None:
            raise ValueError('Книга не найдена в Neo4j')
        book = dict(row['b'])
        result = json.loads(book['metadata'])
        result.update(document_id=book_id, revision=book['revision'], book_title=book['title'])
        sections = tx.run(
            'MATCH (s:BookSection {book_id:$id}) RETURN properties(s) AS s ORDER BY s.start, s.level', id=book_id,
        ).data()
        result['sections'] = []
        for row in sections:
            s = row['s']
            s.pop('book_id', None)
            # Символьные координаты принадлежат графу; чистые алгоритмы пока
            # продолжают работать с индексами элементов из артефакта.
            for key in ('start_char', 'end_char', 'title_position_char',
                        'body_start_char', 'body_end_char'):
                s.pop(key, None)
            for key in ('title', 'title_position', 'end', 'parent_id'):
                s.setdefault(key, None)
            result['sections'].append(s)
        result['scenes'] = {}
        for row in tx.run('MATCH (l:SceneLayer {book_id:$id}) RETURN l', id=book_id):
            layer = dict(row['l'])
            data = json.loads(layer.pop('metadata'))
            chapter_id = layer.pop('chapter_id')
            layer.pop('book_id')
            data.update(layer, items=[])
            result['scenes'][chapter_id] = data
        for row in tx.run(
            'MATCH (s:NarrativeScene {book_id:$id}) RETURN properties(s) AS s ORDER BY s.start_char', id=book_id,
        ):
            scene = row['s']
            scene.pop('book_id')
            result['scenes'][scene['chapter_id']]['items'].append(scene)
        revision = tx.run('MATCH (b:LiteraryBook {id:$id}) RETURN b.revision AS revision', id=book_id).single()
        if revision is None or revision['revision'] != book['revision']:
            raise ConflictError('Книга изменена во время чтения; повтори запрос')
        return result

    def load(self, book_id):
        """Снимок для чистых алгоритмов и UI; инструменты агента читают меньшие области."""
        result = self.load_structure(book_id)
        return result, load_elements(book_id)

    def load_structure(self, book_id):
        """Структура и сцены без загрузки артефакта элементов."""
        return self._read(self._load, book_id)

    @staticmethod
    def _sections(tx, book_id, sections, elements, blocks=()):
        return write_sections(tx, book_id, sections, elements, blocks)

    @staticmethod
    def _layer(tx, book_id, chapter_id, layer, old=None):
        return write_scene_layer(tx, book_id, chapter_id, layer, old)

    def save(self, result, expected_revision):
        """CAS под блокировкой Book: конфликт откатывает всю транзакцию."""
        book_id = result['document_id']
        elements = load_elements(book_id)
        def update(tx):
            row = tx.run('''MATCH (b:LiteraryBook {id:$id})
                SET b.revision=b.revision+1 RETURN b.revision AS revision''', id=book_id).single()
            if row is None:
                raise ValueError('Книга не найдена')
            if row['revision'] != expected_revision + 1:
                raise ConflictError('Книга изменена другим действием. Перечитай её и повтори правку.')
            old = self._load(tx, book_id)
            if old.get('source') != result.get('source'):
                raise ValueError('Нельзя менять исходный текст через правку структуры')
            validate_snapshot(result, elements)
            if old['sections'] != result['sections']:
                write_sections(tx, book_id, result['sections'], elements,
                               result.get('blocks', []))
            else:
                write_content_blocks(tx, book_id, result['sections'],
                                     result.get('blocks', []), elements)
            for chapter, layer in result.get('scenes', {}).items():
                if old.get('scenes', {}).get(chapter) != layer:
                    self._layer(tx, book_id, chapter, layer, old.get('scenes', {}).get(chapter))
            for chapter in old.get('scenes', {}).keys() - result.get('scenes', {}).keys():
                tx.run('''MATCH (c:ChapterEntity {book_id:$id,chapter_id:$chapter})
                    DETACH DELETE c''', id=book_id, chapter=chapter).consume()
                tx.run('''MATCH (e:BookEntity {book_id:$id})
                    WHERE NOT (:ChapterEntity {book_id:$id})-[:RESOLVES_TO]->(e)
                    DETACH DELETE e''', id=book_id).consume()
                tx.run('''MATCH (s:NarrativeScene {book_id:$id,chapter_id:$chapter}),
                                (r:EntityReference {book_id:$id,scene_id:s.id})
                    DETACH DELETE r''', id=book_id, chapter=chapter).consume()
                tx.run('''MATCH (s:NarrativeScene {book_id:$id,chapter_id:$chapter}),
                                (m:EntityMention {book_id:$id,scene_id:s.id})
                    DETACH DELETE m''', id=book_id, chapter=chapter).consume()
                tx.run('''MATCH (s:NarrativeScene {book_id:$id,chapter_id:$chapter}),
                                (e:NarrativeEvent {book_id:$id,scene_id:s.id})
                    DETACH DELETE e''', id=book_id, chapter=chapter).consume()
                tx.run('MATCH (s:NarrativeScene {book_id:$id, chapter_id:$chapter}) DETACH DELETE s', id=book_id, chapter=chapter).consume()
                tx.run('MATCH (c:TextChunk {book_id:$id, chapter_id:$chapter}) DETACH DELETE c',
                       id=book_id, chapter=chapter).consume()
                tx.run('MATCH (l:SceneLayer {book_id:$id, chapter_id:$chapter}) DETACH DELETE l', id=book_id, chapter=chapter).consume()
                rebuild_next_chunk_chain(tx, book_id)
            metadata = {k: v for k, v in result.items() if k not in {'sections', 'scenes'}}
            tx.run('MATCH (b:LiteraryBook {id:$id}) SET b.metadata=$meta, b.title=$title',
                   id=book_id, meta=pack(metadata), title=result['book_title']).consume()
            return row['revision']
        return self._write(update)

    def original_sections(self, book_id):
        return self._read(lambda tx: json.loads(tx.run(
            'MATCH (b:LiteraryBook {id:$id}) RETURN b.original_sections AS sections', id=book_id,
        ).single()['sections']))

    def outline(self, book_id):
        return self._read(lambda tx: tx.run('''MATCH (s:BookSection {book_id:$id})
            OPTIONAL MATCH (s)-[:HAS_BLOCK]->(:SectionBlock {role:'body'})
                -[:HAS_SCENE_LAYER]->(l:SceneLayer)
            RETURN s.id AS id, s.parent_id AS parent_id, s.title AS title, s.role AS role,
            s.start AS start, s.end AS end, s.start_char AS start_char,
            s.end_char AS end_char, s.body_start_char AS body_start_char,
            s.body_end_char AS body_end_char, s.level AS level,
            coalesce(l.status, 'not_started') AS scene_status ORDER BY s.start, s.level''', id=book_id).data())

    def section_range(self, book_id, section_id):
        row = self._read(lambda tx: tx.run('''MATCH (b:LiteraryBook {id:$id})
            MATCH (s:BookSection {book_id:$id,id:$section})
            RETURN s.start_char AS start_char, s.end_char AS end_char,
                   s.start AS start, s.end AS end,
                   b.revision AS revision''', id=book_id, section=section_id).single())
        if row is None:
            raise ValueError('Раздел не найден в этой книге')
        return dict(row)

    def read_text(self, book_id, start_char, end_char, limit=12000):
        if type(start_char) is not int or type(end_char) is not int or not 0 <= start_char < end_char:
            raise ValueError('Неверный диапазон текста')
        if type(limit) is not int or not 1 <= limit <= 50000:
            raise ValueError('limit должен быть от 1 до 50000')
        stop = min(end_char, start_char + limit)
        row = self._read(lambda tx: tx.run('''MATCH (b:LiteraryBook {id:$id})
            RETURN substring(b.text,$start,$length) AS text, size(b.text) AS total''',
            id=book_id, start=start_char, length=stop-start_char).single())
        if row is None or end_char > row['total']:
            raise ValueError('Диапазон отсутствует в книге')
        return dict(book_id=book_id, start_char=start_char, end_char=stop,
                    text=row['text'], next_start=stop if stop < end_char else None)

    def search(self, book_id, query, limit=20):
        if not query.strip() or len(query) > 500 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Нужен поисковый текст до 500 символов и limit от 1 до 100')
        row = self._read(lambda tx: tx.run(
            'MATCH (b:LiteraryBook {id:$id}) RETURN b.text AS text', id=book_id,
        ).single())
        if row is None:
            return []
        text, needle, rows, start = row['text'], query.lower(), [], 0
        lowered = text.lower()
        while len(rows) < limit:
            position = lowered.find(needle, start)
            if position < 0:
                break
            excerpt_start = max(0, position - 300)
            excerpt_end = min(len(text), position + len(query) + 700)
            rows.append(dict(start_char=position, end_char=position + len(query),
                             excerpt=text[excerpt_start:excerpt_end]))
            start = position + max(1, len(query))
        return rows

    def delete(self, book_id):
        def remove(tx):
            tx.run('''MATCH (b:LiteraryBook {id:$id}) SET b.revision=b.revision+1
                WITH b OPTIONAL MATCH (n {book_id:$id})
                WHERE n:TextElement OR n:BookSection OR n:SectionBlock OR n:SceneLayer
                   OR n:NarrativeScene OR n:EntityMention OR n:EntityReference
                   OR n:ChapterEntity OR n:BookEntity
                   OR n:Entity OR n:TextChunk
                   OR n:SearchRun OR n:SearchCandidate
                DETACH DELETE n, b''', id=book_id).consume()
        self._write(remove)


@lru_cache(maxsize=2)
def get_repository(*, initialize=True):
    uri, password = os.getenv('NEO4J_URI'), os.getenv('NEO4J_PASSWORD')
    if not uri or not password:
        raise ValueError('Настрой NEO4J_URI и NEO4J_PASSWORD для работы с книгами')
    driver = GraphDatabase.driver(uri, auth=(os.getenv('NEO4J_USERNAME', 'neo4j'), password),
                                 connection_timeout=10, max_transaction_retry_time=10)
    repository = BookRepository(driver, os.getenv('NEO4J_DATABASE', 'neo4j'))
    try:
        if initialize:
            repository.setup()
        else:
            driver.verify_connectivity()
    except Exception:
        driver.close()
        raise
    return repository
