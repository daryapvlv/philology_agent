"""Neo4j-хранилище нарративной разметки: поля TextChunk и узлы NarrativeEvent."""


class NarrativeRepository:
    def __init__(self, books):
        self.books = books

    def scene_for_annotation(self, book_id, scene_id):
        """Актуальная сцена, её TextChunk по порядку чтения и BookEntity сцены."""
        def read(tx):
            row = tx.run('''MATCH (l:SceneLayer {book_id:$book})-[:HAS_SCENE]->
                                  (s:NarrativeScene {book_id:$book,id:$scene})
                WHERE l.status='ready'
                RETURN properties(s) AS scene''', book=book_id, scene=scene_id).single()
            if row is None:
                raise ValueError('Нужна актуальная полностью построенная сцена')
            scene = dict(row['scene'])
            if not isinstance(scene.get('text'), str) or not scene.get('text_hash'):
                raise ValueError('У сцены отсутствует исходный текст или его hash')
            chunks = tx.run('''MATCH (s:NarrativeScene {book_id:$book,id:$scene})
                    -[:HAS_CHUNK]->(c:TextChunk {book_id:$book})
                RETURN properties(c) AS chunk
                ORDER BY c.start_char,c.end_char,c.id''',
                book=book_id, scene=scene_id).data()
            entities = tx.run('''MATCH (e:BookEntity {book_id:$book})
                    -[:PRESENT_IN]->(:NarrativeScene {book_id:$book,id:$scene})
                RETURN properties(e) AS entity ORDER BY e.canonical_name,e.id''',
                book=book_id, scene=scene_id).data()
            return (scene, [dict(row['chunk']) for row in chunks],
                    [dict(row['entity']) for row in entities])
        return self.books._read(read)

    def annotation_status(self, book_id, scene_id, method_hash):
        """True, если сцена уже размечена этим методом и её текст не менялся —
        повторный вызов не должен тратить LLM-вызов."""
        def read(tx):
            row = tx.run('''MATCH (s:NarrativeScene {book_id:$book,id:$scene})
                RETURN s.text_hash AS text_hash, s.narrative_status AS status,
                       s.narrative_hash AS hash,
                       s.narrative_method_hash AS method_hash''',
                book=book_id, scene=scene_id).single()
            if row is None:
                return False
            return (row['status'] == 'ready' and row['hash'] == row['text_hash']
                    and row['method_hash'] == method_hash)
        return self.books._read(read)

    def replace_annotation(self, book_id, scene, chunk_rows, event_rows, method_hash):
        """Атомарно заменить разметку сцены при неизменном text_hash.

        chunk_rows: [{id, context, representation, speaker_entity_id, level,
        embedded_kind}] — id уже реального TextChunk. event_rows: [{id, ...,
        participants:[{entity_id,role}], from_location, to_location}] — узел
        получает только скалярные поля, participants/from_location/to_location
        становятся связями PARTICIPANT/FROM/TO, а не свойствами узла.
        """
        node_rows = [{key: value for key, value in row.items()
                     if key not in ('participants', 'from_location', 'to_location')}
                    for row in event_rows]

        def write(tx):
            current = tx.run('''MATCH (s:NarrativeScene {book_id:$book,id:$scene})
                RETURN s.text_hash AS text_hash''',
                book=book_id, scene=scene['id']).single()
            if current is None or current['text_hash'] != scene['text_hash']:
                raise ValueError('Сцена изменилась; повтори нарративную разметку')
            tx.run('''UNWIND $rows AS row
                MATCH (c:TextChunk {book_id:$book,id:row.id})
                SET c.narrative_context=row.context,
                    c.narrative_representation=row.representation,
                    c.narrative_speaker_entity_id=row.speaker_entity_id,
                    c.narrative_level=row.level,
                    c.narrative_embedded_kind=row.embedded_kind''',
                book=book_id, rows=chunk_rows).consume()
            tx.run('''MATCH (:NarrativeScene {book_id:$book,id:$scene})
                    -[:HAS_EVENT]->(e:NarrativeEvent {book_id:$book})
                DETACH DELETE e''', book=book_id, scene=scene['id']).consume()
            tx.run('''UNWIND $rows AS row
                MATCH (s:NarrativeScene {book_id:$book,id:$scene})
                CREATE (e:NarrativeEvent) SET e=row
                MERGE (s)-[:HAS_EVENT]->(e)''',
                book=book_id, scene=scene['id'], rows=node_rows).consume()
            tx.run('''UNWIND $rows AS row
                MATCH (e:NarrativeEvent {book_id:$book,id:row.id})
                UNWIND row.participants AS participant
                MATCH (pe:BookEntity {book_id:$book,id:participant.entity_id})
                MERGE (e)-[edge:PARTICIPANT]->(pe)
                SET edge.role=participant.role''',
                book=book_id, rows=event_rows).consume()
            tx.run('''UNWIND $rows AS row WITH row WHERE row.from_location IS NOT NULL
                MATCH (e:NarrativeEvent {book_id:$book,id:row.id}),
                      (loc:BookEntity {book_id:$book,id:row.from_location})
                MERGE (e)-[:FROM]->(loc)''', book=book_id, rows=event_rows).consume()
            tx.run('''UNWIND $rows AS row WITH row WHERE row.to_location IS NOT NULL
                MATCH (e:NarrativeEvent {book_id:$book,id:row.id}),
                      (loc:BookEntity {book_id:$book,id:row.to_location})
                MERGE (e)-[:TO]->(loc)''', book=book_id, rows=event_rows).consume()
            tx.run('''MATCH (s:NarrativeScene {book_id:$book,id:$scene})
                SET s.narrative_status='ready', s.narrative_hash=$hash,
                    s.narrative_method_hash=$method_hash''',
                book=book_id, scene=scene['id'], hash=scene['text_hash'],
                method_hash=method_hash).consume()
        self.books._write(write)

    def preparation_status(self, book_id, chapter_ids, method_hash):
        """Сколько готовых сцен выбранных глав размечено текущим методом и
        сколько связей между событиями в книге — только чтение, для интерфейса."""
        def read(tx):
            scenes = tx.run('''MATCH (l:SceneLayer {book_id:$book,status:'ready'})
                    -[:HAS_SCENE]->(s:NarrativeScene {book_id:$book})
                WHERE l.chapter_id IN $chapters
                RETURN count(DISTINCT s) AS scenes,
                       count(DISTINCT CASE WHEN s.narrative_status='ready'
                                            AND s.narrative_hash=s.text_hash
                                            AND s.narrative_method_hash=$method
                                           THEN s END) AS annotated''',
                book=book_id, chapters=chapter_ids, method=method_hash).single()
            # type(r) вместо -[r:NARRATIVE_LINK]->: пока связей в базе нет, Neo4j
            # предупреждает о неизвестном типе связи на каждом опросе статуса.
            links = tx.run('''OPTIONAL MATCH (:NarrativeEvent {book_id:$book})
                    -[r]->(:NarrativeEvent {book_id:$book})
                WHERE type(r) = 'NARRATIVE_LINK'
                RETURN count(r) AS links''', book=book_id).single()
            scene_links = tx.run('''OPTIONAL MATCH (:NarrativeScene {book_id:$book})
                    -[r]->(:NarrativeScene {book_id:$book})
                WHERE type(r) = 'SCENE_LINK'
                RETURN count(r) AS links''', book=book_id).single()
            return dict(narrative_scenes=scenes['scenes'] if scenes else 0,
                        annotated_scenes=scenes['annotated'] if scenes else 0,
                        narrative_links=links['links'] if links else 0,
                        scene_links=scene_links['links'] if scene_links else 0)
        return self.books._read(read)

    def book_events(self, book_id):
        """Сырые NarrativeEvent уже готовых сцен книги, их участники и чанки
        сцен с нарративной разметкой (representation/level анкера) — без
        вычисления контекста/принадлежности события чанку, это делает чистая
        функция narrative.links.enrich_events."""
        def read(tx):
            scenes = tx.run('''MATCH (l:SceneLayer {book_id:$book,status:'ready'})
                    -[:HAS_SCENE]->(s:NarrativeScene {book_id:$book})
                RETURN s.id AS id, s.title AS title, s.text AS text,
                       s.start_char AS start_char''', book=book_id).data()
            scene_ids = [row['id'] for row in scenes]
            events = tx.run('''MATCH (s:NarrativeScene {book_id:$book})
                    -[:HAS_EVENT]->(e:NarrativeEvent {book_id:$book})
                WHERE s.id IN $scenes
                RETURN properties(e) AS event''', book=book_id, scenes=scene_ids).data()
            participants = tx.run('''MATCH (e:NarrativeEvent {book_id:$book})
                    -[:PARTICIPANT]->(pe:BookEntity {book_id:$book})
                WHERE e.scene_id IN $scenes
                RETURN e.id AS event_id, collect(DISTINCT pe.id) AS entity_ids''',
                book=book_id, scenes=scene_ids).data()
            chunks = tx.run('''MATCH (s:NarrativeScene {book_id:$book})
                    -[:HAS_CHUNK]->(c:TextChunk {book_id:$book})
                WHERE s.id IN $scenes AND c.narrative_level IS NOT NULL
                RETURN s.id AS scene_id, c.start_char AS start_char, c.end_char AS end_char,
                       c.narrative_level AS level,
                       c.narrative_representation AS representation''',
                book=book_id, scenes=scene_ids).data()
            return ([dict(row) for row in scenes], [dict(row['event']) for row in events],
                    {row['event_id']: sorted(row['entity_ids']) for row in participants},
                    [dict(row) for row in chunks])
        return self.books._read(read)

    def link_pairs(self, book_id):
        """Пары событий, уже связанные NARRATIVE_LINK: при пересборке они остаются
        кандидатами и проверяются заново, а не выпадают из-за нового отбора."""
        return self.books._read(lambda tx: [
            (row['a'], row['b']) for row in tx.run('''
                MATCH (a:NarrativeEvent {book_id:$book})-[r]->(b:NarrativeEvent {book_id:$book})
                WHERE type(r) = 'NARRATIVE_LINK'
                RETURN a.id AS a, b.id AS b''', book=book_id).data()])

    def save_event_embeddings(self, book_id, rows, field='gist_roles'):
        """rows: [{id, embedding, model, hash}] — кэш эмбеддингов поля события
        (gist_roles или gist) прямо на NarrativeEvent, только для изменившихся."""
        prefix = {'gist_roles': 'gist_roles_embedding', 'gist': 'gist_embedding'}[field]

        def write(tx):
            tx.run(f'''UNWIND $rows AS row
                MATCH (e:NarrativeEvent {{book_id:$book,id:row.id}})
                SET e.{prefix}=row.embedding,
                    e.{prefix}_model=row.model,
                    e.{prefix}_hash=row.hash''',
                book=book_id, rows=rows).consume()
        self.books._write(write)

    def replace_links(self, book_id, link_rows, method_hash):
        """Полный пересчёт NARRATIVE_LINK по книге — как rebuild_graph: только
        материализация уже классифицированных пар, без LLM в этой транзакции.

        link_rows: [{source_id, target_id, type, subtype, basis, directed, confidence, reason,
        method_hash}].
        """
        def write(tx):
            tx.run('''MATCH (:NarrativeEvent {book_id:$book})-[r:NARRATIVE_LINK]->
                          (:NarrativeEvent {book_id:$book}) DELETE r''',
                book=book_id).consume()
            tx.run('''UNWIND $rows AS row
                MATCH (a:NarrativeEvent {book_id:$book,id:row.source_id}),
                      (b:NarrativeEvent {book_id:$book,id:row.target_id})
                MERGE (a)-[edge:NARRATIVE_LINK]->(b)
                SET edge.type=row.type, edge.subtype=row.subtype, edge.basis=row.basis,
                    edge.directed=row.directed, edge.confidence=row.confidence,
                    edge.reason=row.reason, edge.method_hash=row.method_hash''',
                book=book_id, rows=link_rows).consume()
        self.books._write(write)

    def scene_outline(self, book_id):
        """Готовые сцены книги по порядку чтения для обзора сюжета: название,
        summary, заголовок главы, присутствующие лица и text_hash."""
        def read(tx):
            return [dict(row) for row in tx.run('''
                MATCH (l:SceneLayer {book_id:$book,status:'ready'})
                    -[:HAS_SCENE]->(s:NarrativeScene {book_id:$book})
                OPTIONAL MATCH (chapter:BookSection {book_id:$book,id:l.chapter_id})
                OPTIONAL MATCH (e:BookEntity {book_id:$book,entity_type:'person'})
                    -[:PRESENT_IN]->(s)
                WITH s, chapter, e ORDER BY e.canonical_name
                RETURN s.id AS id, s.title AS title, s.summary AS summary,
                       s.text_hash AS text_hash, s.start_char AS start_char,
                       chapter.title AS chapter,
                       [name IN collect(e.canonical_name) WHERE name IS NOT NULL] AS characters
                ORDER BY s.start_char, s.id''', book=book_id)]
        return self.books._read(read)

    def replace_scene_links(self, book_id, link_rows, method_hash):
        """Полный пересчёт SCENE_LINK по книге. link_rows: [{source_id, target_id,
        type, subtype, directed, basis, reason, found, source_hash, target_hash}] — hash
        текстов сцен на момент обзора: запись отменяется, если сцена успела
        измениться (инвариант 9), а при последующей правке сцены связь удаляет
        инвалидация структуры."""
        def write(tx):
            current = {row['id']: row['hash'] for row in tx.run('''
                MATCH (s:NarrativeScene {book_id:$book}) WHERE s.id IN $ids
                RETURN s.id AS id, s.text_hash AS hash''', book=book_id,
                ids=list({r['source_id'] for r in link_rows} | {r['target_id'] for r in link_rows}))}
            for row in link_rows:
                if (current.get(row['source_id']) != row['source_hash']
                        or current.get(row['target_id']) != row['target_hash']):
                    raise ValueError('Сцены изменились во время обзора сюжета; повтори связи')
            tx.run('''MATCH (:NarrativeScene {book_id:$book})-[r]->(:NarrativeScene {book_id:$book})
                WHERE type(r) = 'SCENE_LINK' DELETE r''', book=book_id).consume()
            tx.run('''UNWIND $rows AS row
                MATCH (a:NarrativeScene {book_id:$book,id:row.source_id}),
                      (b:NarrativeScene {book_id:$book,id:row.target_id})
                MERGE (a)-[edge:SCENE_LINK]->(b)
                SET edge.type=row.type, edge.subtype=row.subtype, edge.basis=row.basis,
                    edge.directed=row.directed, edge.reason=row.reason, edge.found=row.found,
                    edge.source_hash=row.source_hash, edge.target_hash=row.target_hash,
                    edge.method_hash=$method''',
                book=book_id, rows=link_rows, method=method_hash).consume()
        self.books._write(write)
