"""Neo4j-хранилище Entity и EntityMention."""

from structure.graph_db.repository import rebuild_next_scene_chain


class EntityRepository:
    def __init__(self, books):
        self.books = books

    def scene(self, book_id, scene_id):
        def read(tx):
            row = tx.run('''MATCH (l:SceneLayer {book_id:$book})-[:HAS_SCENE]->
                                  (s:NarrativeScene {book_id:$book,id:$scene})
                WHERE l.status='ready'
                RETURN properties(s) AS scene''', book=book_id, scene=scene_id).single()
            if row is None:
                raise ValueError("Нужна актуальная полностью построенная сцена")
            scene = dict(row["scene"])
            if not isinstance(scene.get("text"), str) or not scene.get("text_hash"):
                raise ValueError("У сцены отсутствует исходный текст или его hash")
            return scene
        return self.books._read(read)

    def scene_chunks(self, book_id, scene_id):
        """Вернуть канонические TextChunk сцены в порядке чтения."""
        def read(tx):
            rows = tx.run('''MATCH (l:SceneLayer {book_id:$book,status:'ready'})
                    -[:HAS_SCENE]->(s:NarrativeScene {book_id:$book,id:$scene})
                    -[:HAS_CHUNK]->(c:TextChunk {book_id:$book})
                RETURN properties(c) AS chunk
                ORDER BY c.start_char,c.end_char,c.id''',
                book=book_id, scene=scene_id).data()
            return [dict(row['chunk']) for row in rows]
        return self.books._read(read)

    def replace_mentions(self, book_id, source, mentions, references=()):
        def write(tx):
            row = tx.run('''MATCH (l:SceneLayer {book_id:$book})-[:HAS_SCENE]->
                                  (s:NarrativeScene {book_id:$book,id:$scene})
                WHERE l.status='ready'
                RETURN s.text_hash AS text_hash''',
                book=book_id, scene=source["id"]).single()
            if row is None or row["text_hash"] != source["text_hash"]:
                raise ValueError("Сцена изменилась; повтори extraction")
            tx.run('''MATCH (s:NarrativeScene {book_id:$book,id:$scene})
                SET s.entity_extraction_status='ready',
                    s.entity_extraction_hash=$hash''', book=book_id,
                scene=source['id'], hash=source['text_hash']).consume()
            tx.run('''MATCH (c:ChapterEntity {book_id:$book,chapter_id:$chapter})
                DETACH DELETE c''', book=book_id, chapter=source["chapter_id"]).consume()
            tx.run('''MATCH (chapter:BookSection {book_id:$book,id:$chapter})
                REMOVE chapter.chapter_entity_status''', book=book_id,
                chapter=source['chapter_id']).consume()
            tx.run('''MATCH (r:EntityReference {book_id:$book,scene_id:$scene})
                DETACH DELETE r''', book=book_id, scene=source["id"]).consume()
            ids = [item["id"] for item in mentions]
            tx.run('''MATCH (m:EntityMention {book_id:$book,scene_id:$scene})
                WHERE NOT m.id IN $ids DETACH DELETE m''',
                book=book_id, scene=source["id"], ids=ids).consume()
            tx.run('''UNWIND $rows AS row
                MATCH (s:NarrativeScene {book_id:$book,id:row.scene_id})
                MERGE (m:EntityMention {book_id:$book,id:row.id})
                SET m=row
                MERGE (s)-[:HAS_MENTION]->(m)''', book=book_id, rows=mentions).consume()
            tx.run('''MATCH (s:NarrativeScene {book_id:$book,id:$scene})
                    -[:HAS_MENTION]->(m:EntityMention {book_id:$book})
                MATCH (s)-[:HAS_CHUNK]->(c:TextChunk {book_id:$book})
                WHERE c.start_char < s.start_char+m.end_offset
                  AND c.end_char > s.start_char+m.start_offset
                MERGE (c)-[:CONTAINS_MENTION]->(m)''',
                book=book_id, scene=source['id']).consume()
            tx.run('''UNWIND $rows AS row
                MATCH (s:NarrativeScene {book_id:$book,id:row.scene_id})
                MATCH (m:EntityMention {book_id:$book,id:row.target_mention_id,
                                        scene_id:row.scene_id})
                CREATE (r:EntityReference) SET r=row
                MERGE (s)-[:HAS_REFERENCE]->(r)
                MERGE (r)-[:REFERS_TO_MENTION]->(m)''',
                book=book_id, rows=list(references)).consume()
        self.books._write(write)

    def chapter_evidence(self, book_id, chapter_id):
        def read(tx):
            scenes = tx.run('''MATCH (l:SceneLayer {book_id:$book,chapter_id:$chapter,status:'ready'})
                    -[:HAS_SCENE]->(s:NarrativeScene {book_id:$book,chapter_id:$chapter})
                RETURN properties(s) AS scene ORDER BY s.start_char,s.id''',
                book=book_id, chapter=chapter_id).data()
            if not scenes:
                raise ValueError("У главы нет актуальных готовых сцен")
            mentions = tx.run('''MATCH (s:NarrativeScene {book_id:$book,chapter_id:$chapter})
                    -[:HAS_MENTION]->(m:EntityMention {book_id:$book})
                RETURN properties(m) AS mention, s.start_char AS scene_start
                ORDER BY scene_start,m.start_offset,m.end_offset,m.id''',
                book=book_id, chapter=chapter_id).data()
            references = tx.run('''MATCH (s:NarrativeScene {book_id:$book,chapter_id:$chapter})
                    -[:HAS_REFERENCE]->(r:EntityReference {book_id:$book})
                    -[:REFERS_TO_MENTION]->(m:EntityMention {book_id:$book})
                RETURN properties(r) AS reference, m.id AS target_mention_id,
                       s.start_char AS scene_start
                ORDER BY scene_start,r.start_offset,r.end_offset,r.id''',
                book=book_id, chapter=chapter_id).data()
            return ([dict(row["scene"]) for row in scenes],
                    [dict(row["mention"]) for row in mentions],
                    [dict(row["reference"], target_mention_id=row["target_mention_id"])
                     for row in references])
        return self.books._read(read)

    def replace_chapter_entities(self, book_id, chapter_id, source, entities):
        def write(tx):
            current = tx.run('''MATCH (s:NarrativeScene {book_id:$book,chapter_id:$chapter})
                    -[:HAS_MENTION]->(m:EntityMention {book_id:$book})
                RETURN m.id AS id, m.scene_text_hash AS scene_text_hash
                ORDER BY id''', book=book_id, chapter=chapter_id).data()
            expected = sorted(source, key=lambda row: row["id"])
            if [dict(row) for row in current] != expected:
                raise ValueError("Mentions главы изменились; повтори resolution")
            tx.run('''MATCH (c:ChapterEntity {book_id:$book,chapter_id:$chapter})
                DETACH DELETE c''', book=book_id, chapter=chapter_id).consume()
            tx.run('''MATCH (e:BookEntity {book_id:$book})
                WHERE NOT (:ChapterEntity {book_id:$book})-[:RESOLVES_TO]->(e)
                DETACH DELETE e''', book=book_id).consume()
            tx.run('''UNWIND $rows AS row
                MATCH (chapter:BookSection {book_id:$book,id:$chapter})
                CREATE (c:ChapterEntity) SET c=row
                MERGE (chapter)-[:HAS_CHAPTER_ENTITY]->(c)''',
                book=book_id, chapter=chapter_id, rows=entities).consume()
            tx.run('''UNWIND $rows AS row
                MATCH (c:ChapterEntity {book_id:$book,id:row.id})
                UNWIND row.mention_ids AS mention_id
                MATCH (m:EntityMention {book_id:$book,id:mention_id})
                MERGE (m)-[:IN_CHAPTER_ENTITY]->(c)''',
                book=book_id, rows=entities).consume()
            tx.run('''MATCH (chapter:BookSection {book_id:$book,id:$chapter})
                SET chapter.chapter_entity_status='ready',
                    chapter.chapter_entity_mentions=$mentions''',
                book=book_id, chapter=chapter_id, mentions=len(source)).consume()
        self.books._write(write)

    def preparation_status(self, book_id, chapter_ids):
        """Готовность технических слоёв в выбранных главах."""
        def read(tx):
            per_chapter = tx.run('''UNWIND range(0,size($chapters)-1) AS chapter_index
                WITH chapter_index,$chapters[chapter_index] AS chapter_id
                OPTIONAL MATCH (l:SceneLayer {book_id:$book,chapter_id:chapter_id})
                OPTIONAL MATCH (l)-[:HAS_SCENE]->(s:NarrativeScene {book_id:$book})
                OPTIONAL MATCH (s)-[:HAS_CHUNK]->(c:TextChunk {book_id:$book})
                OPTIONAL MATCH (chapter:BookSection {book_id:$book,id:chapter_id})
                RETURN chapter_index AS chapter_index,chapter_id AS chapter_id,
                       coalesce(l.status,'not_started') AS scene_status,
                       count(DISTINCT s) AS scenes,
                       count(DISTINCT c) AS chunks,
                       count(DISTINCT CASE WHEN c.embedding IS NOT NULL THEN c END)
                           AS embedded_chunks,
                       count(DISTINCT CASE
                           WHEN s.entity_extraction_status='ready'
                            AND s.entity_extraction_hash=s.text_hash THEN s END)
                           AS extracted_scenes,
                       coalesce(chapter.chapter_entity_status,'not_started')
                           AS chapter_entity_status
                ORDER BY chapter_index''', book=book_id, chapters=chapter_ids).data()
            layers = tx.run('''MATCH (l:SceneLayer {book_id:$book})
                WHERE l.chapter_id IN $chapters AND l.status='ready'
                RETURN count(DISTINCT l.chapter_id) AS chapters''',
                book=book_id, chapters=chapter_ids).single()
            scenes = tx.run('''MATCH (l:SceneLayer {book_id:$book,status:'ready'})
                    -[:HAS_SCENE]->(s:NarrativeScene {book_id:$book})
                WHERE l.chapter_id IN $chapters
                OPTIONAL MATCH (s)-[:HAS_CHUNK]->(c:TextChunk {book_id:$book})
                RETURN count(DISTINCT s) AS scenes,count(DISTINCT c) AS chunks,
                       count(DISTINCT CASE WHEN c.embedding IS NOT NULL THEN c END)
                           AS embedded_chunks,
                       count(DISTINCT CASE WHEN s.summary_embedding IS NOT NULL THEN s END)
                           AS embedded_scenes,
                       count(DISTINCT CASE
                           WHEN s.entity_extraction_status='ready'
                            AND s.entity_extraction_hash=s.text_hash THEN s END)
                           AS extracted_scenes''',
                book=book_id, chapters=chapter_ids).single()
            resolved = tx.run('''MATCH (chapter:BookSection {book_id:$book})
                WHERE chapter.id IN $chapters AND chapter.chapter_entity_status='ready'
                RETURN count(chapter) AS chapters''',
                book=book_id, chapters=chapter_ids).single()
            graph = tx.run('''MATCH (e:BookEntity {book_id:$book})
                WITH count(e) AS entities
                OPTIONAL MATCH (:BookEntity {book_id:$book})-[r:PRESENT_IN]->
                    (s:NarrativeScene {book_id:$book})
                WHERE s.chapter_id IN $chapters
                RETURN entities,count(r) AS presence''',
                book=book_id, chapters=chapter_ids).single()
            return dict(
                chapters=len(chapter_ids),
                scene_chapters=layers['chapters'] if layers else 0,
                scenes=scenes['scenes'] if scenes else 0,
                chunks=scenes['chunks'] if scenes else 0,
                embedded_chunks=scenes['embedded_chunks'] if scenes else 0,
                embedded_scenes=scenes['embedded_scenes'] if scenes else 0,
                extracted_scenes=scenes['extracted_scenes'] if scenes else 0,
                chapter_entity_chapters=resolved['chapters'] if resolved else 0,
                book_entities=graph['entities'] if graph else 0,
                presence_edges=graph['presence'] if graph else 0,
                chapter_rows=[dict(row) for row in per_chapter],
            )
        return self.books._read(read)

    def chapter_entities(self, book_id, chapter_id):
        entities = self.books._read(lambda tx: [dict(row["entity"]) for row in tx.run('''
            MATCH (c:ChapterEntity {book_id:$book,chapter_id:$chapter})
            RETURN properties(c) AS entity ORDER BY c.id''',
            book=book_id, chapter=chapter_id)])
        return sorted(entities, key=lambda item: (item.get("canonical_name", ""), item["id"]))

    def book_resolution_evidence(self, book_id, *, include_resolved=False):
        """Вернуть ChapterEntity для incremental resolve или полного rebuild."""
        def read(tx):
            pending = tx.run('''MATCH (chapter:BookSection {book_id:$book})
                    -[:HAS_CHAPTER_ENTITY]->(c:ChapterEntity {book_id:$book})
                OPTIONAL MATCH (c)-[:RESOLVES_TO]->(target:BookEntity {book_id:$book})
                WITH chapter,c,target WHERE $all OR target IS NULL
                RETURN properties(c) AS entity
                ORDER BY chapter.start,chapter.start_char,c.id''',
                book=book_id, all=include_resolved).data()
            existing = tx.run('''MATCH (:ChapterEntity {book_id:$book})
                    -[:RESOLVES_TO]->(e:BookEntity {book_id:$book})
                RETURN DISTINCT properties(e) AS entity ORDER BY entity.id''',
                book=book_id).data()
            return ([dict(row["entity"]) for row in pending],
                    [dict(row["entity"]) for row in existing])
        return self.books._read(read)

    def replace_book_resolution(self, book_id, sources, decisions, entities):
        """Атомарно заменить полный ChapterEntity -> BookEntity слой книги."""
        def write(tx):
            current = tx.run('''MATCH (chapter:BookSection {book_id:$book})
                    -[:HAS_CHAPTER_ENTITY]->(c:ChapterEntity {book_id:$book})
                RETURN c.id AS id,c.mention_ids AS mention_ids
                ORDER BY chapter.start,chapter.start_char,c.id''', book=book_id).data()
            expected = [{"id": row["id"],
                         "mention_ids": list(row.get("mention_ids", []))}
                        for row in sources]
            actual = [{"id": row["id"],
                       "mention_ids": list(row["mention_ids"] or [])}
                      for row in current]
            if actual != expected:
                raise ValueError("ChapterEntity изменились; повтори book resolution")
            tx.run('''MATCH (:ChapterEntity {book_id:$book})-[r:RESOLVES_TO]->
                           (:BookEntity {book_id:$book}) DELETE r''', book=book_id).consume()
            tx.run('''MATCH (e:BookEntity {book_id:$book}) DETACH DELETE e''',
                   book=book_id).consume()
            tx.run('''UNWIND $rows AS row
                MATCH (b:LiteraryBook {id:$book})
                CREATE (e:BookEntity) SET e=row
                MERGE (b)-[:HAS_BOOK_ENTITY]->(e)''',
                book=book_id, rows=entities).consume()
            tx.run('''UNWIND $rows AS row
                MATCH (c:ChapterEntity {book_id:$book,id:row.chapter_entity_id})
                SET c.book_resolution_state=row.decision
                WITH c,row WHERE row.entity_id IS NOT NULL
                MATCH (e:BookEntity {book_id:$book,id:row.entity_id})
                MERGE (c)-[:RESOLVES_TO]->(e)''',
                book=book_id, rows=decisions).consume()
        self.books._write(write)

    def apply_book_resolution_batch(self, book_id, sources, decisions, entities):
        """Атомарно применить один batch ChapterEntity -> BookEntity."""
        def write(tx):
            for source in sources:
                current = tx.run('''MATCH (c:ChapterEntity {book_id:$book,id:$id})
                    RETURN c.mention_ids AS mention_ids''', book=book_id,
                    id=source["id"]).single()
                if current is None or list(current["mention_ids"] or []) != list(
                        source.get("mention_ids", [])):
                    raise ValueError("ChapterEntity изменилась; повтори book resolution")
            tx.run('''UNWIND $rows AS row
                MATCH (b:LiteraryBook {id:$book})
                MERGE (e:BookEntity {book_id:$book,id:row.id}) SET e=row
                MERGE (b)-[:HAS_BOOK_ENTITY]->(e)''',
                book=book_id, rows=entities).consume()
            for row in decisions:
                tx.run('''MATCH (c:ChapterEntity {book_id:$book,id:$source})
                    OPTIONAL MATCH (c)-[old:RESOLVES_TO]->(:BookEntity) DELETE old
                    SET c.book_resolution_state=$decision''', book=book_id,
                    source=row["chapter_entity_id"], decision=row["decision"]).consume()
                if row.get("entity_id") is not None:
                    target = tx.run('''MATCH (e:BookEntity {book_id:$book,id:$entity})
                        RETURN e.id AS id''', book=book_id,
                        entity=row["entity_id"]).single()
                    if target is None:
                        raise ValueError("BookEntity не найдена в этой книге")
                    tx.run('''MATCH (c:ChapterEntity {book_id:$book,id:$source}),
                                    (e:BookEntity {book_id:$book,id:$entity})
                        MERGE (c)-[:RESOLVES_TO]->(e)''', book=book_id,
                        source=row["chapter_entity_id"], entity=row["entity_id"]).consume()
        self.books._write(write)

    def merge_book_entities(self, book_id, survivor_id, loser_ids, merged_entity):
        """Слить несколько BookEntity в survivor: перевесить ChapterEntity
        RESOLVES_TO на survivor, обновить его поля, удалить проигравшие узлы.
        PRESENT_IN не трогаем — его целиком пересчитывает rebuild_graph."""
        def write(tx):
            tx.run('''UNWIND $losers AS loser
                MATCH (c:ChapterEntity {book_id:$book})-[r:RESOLVES_TO]->
                      (:BookEntity {book_id:$book,id:loser})
                MATCH (survivor:BookEntity {book_id:$book,id:$survivor})
                DELETE r
                MERGE (c)-[:RESOLVES_TO]->(survivor)''',
                book=book_id, losers=loser_ids, survivor=survivor_id).consume()
            tx.run('''MATCH (e:BookEntity {book_id:$book,id:$survivor}) SET e=$row''',
                book=book_id, survivor=survivor_id, row=merged_entity).consume()
            tx.run('''UNWIND $losers AS loser
                MATCH (e:BookEntity {book_id:$book,id:loser}) DETACH DELETE e''',
                book=book_id, losers=loser_ids).consume()
        self.books._write(write)

    def book_entities(self, book_id):
        return self.books._read(lambda tx: [dict(row["entity"]) for row in tx.run('''
            MATCH (:ChapterEntity {book_id:$book})-[:RESOLVES_TO]->
                  (e:BookEntity {book_id:$book})
            RETURN DISTINCT properties(e) AS entity ORDER BY entity.id''', book=book_id)])

    def save_possibly_same(self, book_id, pairs):
        """POSSIBLY_SAME {score}: полный пересчёт по книге из UNCERTAIN-пар
        последней book reconciliation — как rebuild_graph, только материализация
        уже посчитанной эвристической оценки, без LLM в этой транзакции.

        pairs: [{left_entity_id, right_entity_id, score}] с left/right в
        каноническом порядке (left_entity_id < right_entity_id) — ребро
        всегда пишется и удаляется в этом одном направлении, само отношение
        читается как ненаправленное. Пара, где одна из сторон уже слита в
        survivor другим merge этого же прохода, просто не находит второй узел
        и не создаёт ребро — не ошибка."""
        def write(tx):
            tx.run('''MATCH (:BookEntity {book_id:$book})-[r:POSSIBLY_SAME]->
                          (:BookEntity {book_id:$book}) DELETE r''',
                book=book_id).consume()
            tx.run('''UNWIND $rows AS row
                MATCH (a:BookEntity {book_id:$book,id:row.left_entity_id}),
                      (b:BookEntity {book_id:$book,id:row.right_entity_id})
                MERGE (a)-[edge:POSSIBLY_SAME]->(b)
                SET edge.score=row.score''', book=book_id, rows=pairs).consume()
        self.books._write(write)

    def rebuild_graph(self, book_id):
        """Материализовать Graph v1 только из уже сохранённого evidence."""
        def write(tx):
            tx.run('''MATCH (:BookEntity {book_id:$book})-[r:PRESENT_IN]->
                           (:NarrativeScene) DELETE r''', book=book_id).consume()
            presence = tx.run('''MATCH (l:SceneLayer {book_id:$book,status:'ready'})
                    -[:HAS_SCENE]->(s:NarrativeScene {book_id:$book})
                MATCH (s)-[:HAS_MENTION]->(m:EntityMention {book_id:$book})
                    -[:IN_CHAPTER_ENTITY]->(c:ChapterEntity {book_id:$book})
                    -[:RESOLVES_TO]->(e:BookEntity {book_id:$book})
                WITH e,s,count(DISTINCT m) AS mention_count
                OPTIONAL MATCH (s)-[:HAS_REFERENCE]->
                    (reference:EntityReference {book_id:$book})
                    -[:REFERS_TO_MENTION]->(anchor:EntityMention {book_id:$book})
                    -[:IN_CHAPTER_ENTITY]->(:ChapterEntity {book_id:$book})
                    -[:RESOLVES_TO]->(e)
                WITH e,s,mention_count,count(DISTINCT reference) AS reference_count
                MERGE (e)-[edge:PRESENT_IN]->(s)
                SET edge.book_entity_id=e.id, edge.scene_id=s.id,
                    edge.mention_count=mention_count,
                    edge.reference_count=reference_count
                RETURN count(edge) AS count''', book=book_id).single()

            # NEXT_SCENE уже поддерживается независимо от entity resolution
            # (structure.graph_db.repository.write_scene_layer вызывает
            # rebuild_next_scene_chain при каждом сохранении слоя сцен); здесь
            # достаточно убедиться, что цепочка отражает текущий набор сцен.
            next_edges = rebuild_next_scene_chain(tx, book_id)
            scene_count = tx.run('''MATCH (l:SceneLayer {book_id:$book,status:'ready'})
                    -[:HAS_SCENE]->(s:NarrativeScene {book_id:$book})
                RETURN count(DISTINCT s) AS count''', book=book_id).single()
            return {
                "presence_edges": presence["count"] if presence else 0,
                "next_edges": next_edges,
                "scenes": scene_count["count"] if scene_count else 0,
            }
        return self.books._write(write)

    def scenes_for_entity(self, book_id, book_entity_id):
        return self.books._read(lambda tx: [{
            **dict(row["scene"]),
            "mention_count": row["mention_count"],
            "reference_count": row["reference_count"],
        } for row in tx.run('''MATCH (e:BookEntity {book_id:$book,id:$entity})
                -[edge:PRESENT_IN]->(s:NarrativeScene {book_id:$book})
            RETURN properties(s) AS scene, edge.mention_count AS mention_count,
                   edge.reference_count AS reference_count
            ORDER BY s.start_char,s.end_char,s.id''',
            book=book_id, entity=book_entity_id)])

    def entities_for_scene(self, book_id, scene_id):
        return self.books._read(lambda tx: [{
            **dict(row["entity"]),
            "mention_count": row["mention_count"],
            "reference_count": row["reference_count"],
        } for row in tx.run('''MATCH (e:BookEntity {book_id:$book})
                -[edge:PRESENT_IN]->(s:NarrativeScene {book_id:$book,id:$scene})
            RETURN properties(e) AS entity, edge.mention_count AS mention_count,
                   edge.reference_count AS reference_count
            ORDER BY e.canonical_name,e.id''', book=book_id, scene=scene_id)])

    def adjacent_scene(self, book_id, scene_id, *, previous=False):
        pattern = (("(adjacent:NarrativeScene {book_id:$book})-[:NEXT_SCENE]->"
                    "(s:NarrativeScene {book_id:$book,id:$scene})") if previous else
                   ("(s:NarrativeScene {book_id:$book,id:$scene})-[:NEXT_SCENE]->"
                    "(adjacent:NarrativeScene {book_id:$book})"))
        row = self.books._read(lambda tx: tx.run(
            f"MATCH {pattern} RETURN properties(adjacent) AS scene",
            book=book_id, scene=scene_id,
        ).single())
        return dict(row["scene"]) if row else None

    def mentions(self, book_id, scene_id=None, *, unresolved_only=False):
        def read(tx):
            return tx.run('''MATCH (l:SceneLayer {book_id:$book,status:'ready'})
                    -[:HAS_SCENE]->(s:NarrativeScene)-[:HAS_MENTION]->
                    (m:EntityMention {book_id:$book})
                WHERE ($scene IS NULL OR s.id=$scene)
                OPTIONAL MATCH (m)-[:REFERS_TO]->(e:Entity {book_id:$book})
                WITH m,e WHERE NOT $unresolved OR e IS NULL
                RETURN properties(m) AS mention, e.id AS entity_id
                ORDER BY m.scene_id,m.start_offset,m.end_offset''',
                book=book_id, scene=scene_id, unresolved=unresolved_only).data()
        return [dict(row["mention"], entity_id=row["entity_id"])
                for row in self.books._read(read)]

    def references(self, book_id, scene_id=None):
        return self.books._read(lambda tx: [dict(row["reference"]) for row in tx.run('''
            MATCH (s:NarrativeScene {book_id:$book})-[:HAS_REFERENCE]->
                  (r:EntityReference {book_id:$book})-[:REFERS_TO_MENTION]->
                  (m:EntityMention {book_id:$book})
            WHERE $scene IS NULL OR s.id=$scene
            RETURN properties(r) AS reference
            ORDER BY r.scene_id,r.start_offset,r.end_offset,r.id''',
            book=book_id, scene=scene_id)])

    def entities(self, book_id):
        return self.books._read(lambda tx: [row["entity"] for row in tx.run('''
            MATCH (e:Entity {book_id:$book}) RETURN properties(e) AS entity
            ORDER BY e.canonical_name,e.id''', book=book_id)])

    def apply_resolutions(self, book_id, resolutions, entities):
        def write(tx):
            if tx.run('MATCH (b:LiteraryBook {id:$book}) RETURN b.id',
                      book=book_id).single() is None:
                raise ValueError("Книга не найдена")
            tx.run('''UNWIND $rows AS row
                MATCH (b:LiteraryBook {id:$book})
                MERGE (e:Entity {book_id:$book,id:row.id}) SET e=row
                MERGE (b)-[:HAS_ENTITY]->(e)''', book=book_id, rows=entities).consume()
            for row in resolutions:
                mention = tx.run('''MATCH (m:EntityMention {book_id:$book,id:$mention})
                    RETURN m''', book=book_id, mention=row["mention_id"]).single()
                if mention is None:
                    raise ValueError("Mention не найден в этой книге")
                tx.run('''MATCH (m:EntityMention {book_id:$book,id:$mention})
                    OPTIONAL MATCH (m)-[r:REFERS_TO]->(:Entity) DELETE r''',
                    book=book_id, mention=row["mention_id"]).consume()
                if row["entity_id"] is not None:
                    target = tx.run('''MATCH (e:Entity {book_id:$book,id:$entity})
                        RETURN e.id''', book=book_id, entity=row["entity_id"]).single()
                    if target is None:
                        raise ValueError("Entity не найдена в этой книге")
                    tx.run('''MATCH (m:EntityMention {book_id:$book,id:$mention}),
                                    (e:Entity {book_id:$book,id:$entity})
                        MERGE (m)-[:REFERS_TO]->(e)''', book=book_id,
                        mention=row["mention_id"], entity=row["entity_id"]).consume()
        self.books._write(write)

    def link(self, book_id, mention_id, entity_id):
        self.apply_resolutions(book_id, [{"mention_id": mention_id,
                                          "entity_id": entity_id}], [])

    def unlink(self, book_id, mention_id):
        self.apply_resolutions(book_id, [{"mention_id": mention_id,
                                          "entity_id": None}], [])
