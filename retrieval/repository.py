"""Только чтение Neo4j: независимые стратегии поиска и контекст."""

from .ranges import searchable_ranges


class SearchRepository:
    def __init__(self, books):
        self.books = books

    @staticmethod
    def _scope(tx, book_id, section_ids):
        book = tx.run('''MATCH (b:LiteraryBook {id:$id})
            RETURN b.revision AS revision, size(b.text) AS length,
                   b.search_generation AS generation,
                   b.search_embedding_model AS model,
                   b.search_chunk_count AS chunk_count, b.search_embedded_count AS embedded_count,
                   coalesce(b.search_scene_count,0) AS scene_count,
                   coalesce(b.search_scene_embedded_count,0) AS scene_embedded_count,
                   b.search_dimensions AS dimensions,
                   b.search_text_hash=b.text_hash AS current''', id=book_id).single()
        if book is None:
            raise ValueError('Книга не найдена')
        sections = tx.run('''MATCH (s:BookSection {book_id:$id})
            OPTIONAL MATCH (s)-[:HAS_BLOCK]->(body:SectionBlock {role:'body'})
            RETURN s.id AS id, s.title AS title, s.role AS role, s.level AS level,
                   s.parent_id AS parent_id,
                   s.start_char AS start_char, s.end_char AS end_char,
                   body.start_char AS body_start_char,body.end_char AS body_end_char,
                   [(s)-[:HAS_BLOCK]->(x:SectionBlock)
                    WHERE x.role IN ['epigraph','footnotes'] |
                    {start:x.start_char,end:x.end_char}] AS excluded_ranges''', id=book_id).data()
        if section_ids:
            selected = [s for s in sections if s['id'] in section_ids]
            if len(selected) != len(set(section_ids)):
                raise ValueError('Раздел отсутствует в книге или его конец ещё не определён')
        ranges = searchable_ranges(sections, section_ids)
        if not ranges:
            raise ValueError('В выбранной области нет основного текста (body)')
        return dict(book), sections, ranges

    def scope(self, book_id, section_ids):
        return self.books._read(self._scope, book_id, section_ids)

    def words(self, book_id, ranges, terms, target, limit):
        name, kind = {'text': ('retrieval_text', 'chunk'), 'scenes': ('retrieval_scenes', 'scene'),
                     'context': ('retrieval_context', 'chunk')}[target]
        def read(tx):
            rows = tx.run('''CALL db.index.fulltext.queryNodes($index,$terms)
            YIELD node, score
            WHERE node.book_id=$id
              AND any(r IN $ranges WHERE node.start_char<r.end AND node.end_char>r.start)
              AND ($kind='chunk' OR (coalesce(node.description_stale,false)=false AND EXISTS {
                  MATCH (l:SceneLayer {book_id:$id})-[:HAS_SCENE]->(node)
                  WHERE l.status IN ['ready','partial'] }))
            RETURN node.id AS id, node.start_char AS start_char,
                   node.end_char AS end_char, score
            ORDER BY score DESC, id LIMIT $limit''',
            index=name, terms=terms, id=book_id, ranges=ranges, kind=kind, limit=limit+1).data()
            return rows
        return self.books._read(read)

    def semantic(self, book_id, ranges, vector, model, limit):
        def read(tx):
            rows = tx.run('''MATCH (c:TextChunk {book_id:$id})
            WHERE c.embedding_model=$model AND size(c.embedding)=size($vector)
              AND any(r IN $ranges WHERE c.start_char<r.end AND c.end_char>r.start)
            WITH c, vector.similarity.cosine(c.embedding,$vector) AS score
            RETURN c.id AS id, c.start_char AS start_char, c.end_char AS end_char, score
            ORDER BY score DESC, id LIMIT $limit''',
            id=book_id, model=model, vector=vector, ranges=ranges, limit=limit+1).data()
            return rows
        return self.books._read(read)

    def scene_summaries(self, book_id, ranges, vector, model, limit):
        """Ранжирование описаний сцен; результат — диапазоны сцен, не отрывки."""
        return self.books._read(lambda tx: tx.run('''
            MATCH (l:SceneLayer {book_id:$id})-[:HAS_SCENE]->(s:NarrativeScene {book_id:$id})
            WHERE l.status IN ['ready','partial']
              AND any(r IN $ranges WHERE s.start_char<r.end AND s.end_char>r.start)
              AND s.summary_embedding_model=$model
              AND size(s.summary_embedding)=size($vector)
            WITH s,vector.similarity.cosine(s.summary_embedding,$vector) AS score
            RETURN s.id AS id,s.start_char AS start_char,s.end_char AS end_char,score
            ORDER BY score DESC,id LIMIT $limit''', id=book_id, ranges=ranges,
            vector=vector, model=model, limit=limit + 1).data())

    def passages_overlapping(self, book_id, spans):
        """TextChunk кандидата; для сцен используется явная HAS_CHUNK-связь."""
        return self.books._read(lambda tx: tx.run('''
            UNWIND $spans AS span
            MATCH (c:TextChunk {book_id:$id})
            WHERE (size(coalesce(span.scene_ids,[])) > 0 AND EXISTS {
                    MATCH (s:NarrativeScene {book_id:$id})-[:HAS_CHUNK]->(c)
                    WHERE s.id IN span.scene_ids
                  })
               OR (size(coalesce(span.scene_ids,[])) = 0
                   AND c.start_char<span.end AND c.end_char>span.start)
            RETURN DISTINCT span.id AS span_id, c.id AS id,
                   c.start_char AS start_char, c.end_char AS end_char
            ORDER BY start_char, id''', id=book_id, spans=spans).data())

    def entity_catalog(self, book_id):
        return self.books._read(lambda tx: tx.run('''
            MATCH (e:BookEntity {book_id:$id})
            OPTIONAL MATCH (e)-[:PRESENT_IN]->(s:NarrativeScene {book_id:$id})
            RETURN e.id AS id,e.canonical_name AS canonical_name,
                   coalesce(e.aliases,[]) AS aliases,e.entity_type AS entity_type,
                   count(DISTINCT s) AS scene_count
            ORDER BY canonical_name,id''', id=book_id).data())

    def entity_ids(self, book_id, entity_ids):
        return self.books._read(lambda tx: [row['id'] for row in tx.run('''
            MATCH (e:BookEntity {book_id:$book}) WHERE e.id IN $ids
            RETURN e.id AS id''', book=book_id, ids=entity_ids)])

    def entity_scenes(self, book_id, entity_ids, mode, ranges, limit):
        return self.books._read(lambda tx: tx.run('''
            MATCH (e:BookEntity {book_id:$book})-[:PRESENT_IN]->
                  (s:NarrativeScene {book_id:$book})
            WHERE e.id IN $ids
              AND any(r IN $ranges WHERE s.start_char<r.end AND s.end_char>r.start)
            WITH s,collect(DISTINCT e.id) AS matched
            WHERE $mode='union' OR size(matched)=size($ids)
            OPTIONAL MATCH (s)-[:HAS_MENTION]->(m:EntityMention {book_id:$book})
                  -[:IN_CHAPTER_ENTITY]->(:ChapterEntity {book_id:$book})
                  -[:RESOLVES_TO]->(mentioned:BookEntity {book_id:$book})
            WHERE mentioned.id IN $ids
            RETURN s.id AS id,s.start_char AS start_char,s.end_char AS end_char,
                   size(matched) AS matched_entities,
                   min(s.start_char+m.start_offset) AS anchor_start_char,
                   min(s.start_char+m.end_offset) AS anchor_end_char
            ORDER BY s.start_char,s.id LIMIT $limit''', book=book_id, ids=entity_ids,
            mode=mode, ranges=ranges, limit=limit + 1).data())

    def neighbors(self, book_id, scene_id, before, after, ranges):
        def read(tx):
            anchor = tx.run('''MATCH (l:SceneLayer {book_id:$book})-[:HAS_SCENE]->
                    (s:NarrativeScene {book_id:$book,id:$scene})
                WHERE l.status IN ['ready','partial']
                RETURN s.start_char AS start_char''', book=book_id, scene=scene_id).single()
            if anchor is None:
                return None
            previous = tx.run('''MATCH (l:SceneLayer {book_id:$book})-[:HAS_SCENE]->
                    (s:NarrativeScene {book_id:$book})
                WHERE l.status IN ['ready','partial'] AND s.start_char<$start
                  AND any(r IN $ranges WHERE s.start_char<r.end AND s.end_char>r.start)
                RETURN s.id AS id,s.start_char AS start_char,s.end_char AS end_char
                ORDER BY s.start_char DESC,s.id LIMIT $limit''', book=book_id,
                start=anchor['start_char'], ranges=ranges, limit=before).data()
            following = tx.run('''MATCH (l:SceneLayer {book_id:$book})-[:HAS_SCENE]->
                    (s:NarrativeScene {book_id:$book})
                WHERE l.status IN ['ready','partial'] AND s.start_char>$start
                  AND any(r IN $ranges WHERE s.start_char<r.end AND s.end_char>r.start)
                RETURN s.id AS id,s.start_char AS start_char,s.end_char AS end_char
                ORDER BY s.start_char,s.id LIMIT $limit''', book=book_id,
                start=anchor['start_char'], ranges=ranges, limit=after).data()
            return list(reversed(previous)) + following
        return self.books._read(read)

    def narrative_links_status(self, book_id):
        """Есть ли в книге хоть одна связь NARRATIVE_LINK или SCENE_LINK — сигнал для
        retelling/prefiguring/parallel/causal каналов expand_narrative, как
        entity_graph_status для character/chronotope."""
        return self.books._read(lambda tx: tx.run('''
            OPTIONAL MATCH (:NarrativeEvent {book_id:$id})-[l]->
                (:NarrativeEvent {book_id:$id})
            WHERE type(l) = 'NARRATIVE_LINK'
            WITH count(l) AS events
            OPTIONAL MATCH (:NarrativeScene {book_id:$id})-[l]->
                (:NarrativeScene {book_id:$id})
            WHERE type(l) = 'SCENE_LINK'
            WITH events, count(l) AS scenes
            RETURN events + scenes > 0 AS ready''', id=book_id).single()['ready'])

    def entity_graph_status(self, book_id):
        """Сколько ready-сцен уже покрыты entity-графом (PRESENT_IN).

        Используется только index_status: сигнал для character/chronotope
        каналов expand_narrative, не влияет на обычный поиск.
        """
        return self.books._read(lambda tx: dict(tx.run('''
            MATCH (l:SceneLayer {book_id:$id,status:'ready'})-[:HAS_SCENE]->(s:NarrativeScene)
            WITH count(DISTINCT s) AS ready_scenes
            OPTIONAL MATCH (:BookEntity {book_id:$id})-[:PRESENT_IN]->
                (covered:NarrativeScene {book_id:$id})
            RETURN ready_scenes, count(DISTINCT covered) AS covered_scenes''',
            id=book_id).single()))

    def scene_ranges(self, book_id, scene_ids):
        """Диапазоны заданных ready/partial сцен, без ограничения по области."""
        return self.books._read(lambda tx: tx.run('''
            MATCH (l:SceneLayer {book_id:$id})-[:HAS_SCENE]->(s:NarrativeScene)
            WHERE l.status IN ['ready','partial'] AND s.id IN $scenes
            RETURN s.id AS id, s.start_char AS start_char, s.end_char AS end_char''',
            id=book_id, scenes=scene_ids).data())

    def scene_ids(self, book_id, scene_ids, ranges):
        return self.books._read(lambda tx: [r['id'] for r in tx.run('''
            MATCH (l:SceneLayer {book_id:$id})-[:HAS_SCENE]->(s:NarrativeScene)
            WHERE l.status IN ['ready','partial'] AND s.id IN $scene_ids
              AND any(r IN $ranges WHERE s.start_char<r.end AND s.end_char>r.start)
            RETURN s.id AS id''', id=book_id, scene_ids=scene_ids, ranges=ranges)])

    def related_scenes(self, book_id, spans, scene_ids, ranges, hops, limit):
        if type(hops) is not int or not 0 <= hops <= 3:
            raise ValueError('hops должен быть от 0 до 3')
        # В Cypher длина пути — литерал; подставляем только проверенное целое.
        return self.books._read(lambda tx: tx.run(f'''
            MATCH (l:SceneLayer {{book_id:$id}})-[:HAS_SCENE]->(s:NarrativeScene)
            WHERE l.status IN ['ready','partial'] AND (s.id IN $scene_ids OR
                any(span IN $spans WHERE s.start_char<span.end AND s.end_char>span.start))
            MATCH path=(s)-[:NEXT_SCENE*0..{hops}]-(n:NarrativeScene {{book_id:$id}})
            WHERE any(r IN $ranges WHERE n.start_char<r.end AND n.end_char>r.start)
            RETURN n.id AS id, n.start_char AS start_char, n.end_char AS end_char,
                   min(length(path)) AS distance
            ORDER BY distance, start_char, id LIMIT $limit''',
            id=book_id, spans=spans, scene_ids=scene_ids, ranges=ranges, limit=limit+1).data())

    def scenes_in_ranges(self, book_id, ranges, limit):
        """Все ready/partial сцены, пересекающиеся с ranges, в порядке чтения.

        Используется каналом `composition`: ranges — поддерево структурного
        предка (обычно на несколько уровней выше главы анкора), полученное
        через `retrieval.ranges.searchable_ranges`, без LLM.
        """
        return self.books._read(lambda tx: tx.run('''
            MATCH (l:SceneLayer {book_id:$id})-[:HAS_SCENE]->(s:NarrativeScene)
            WHERE l.status IN ['ready','partial']
              AND any(r IN $ranges WHERE s.start_char<r.end AND s.end_char>r.start)
            RETURN s.id AS id, s.start_char AS start_char, s.end_char AS end_char
            ORDER BY s.start_char, s.id LIMIT $limit''',
            id=book_id, ranges=ranges, limit=limit + 1).data())

    def entities_for_scenes(self, book_id, scene_ids, entity_types):
        """BookEntity, присутствующие в заданных сценах, опционально по типу.

        Используется каналами `character`/`chronotope`, чтобы найти сущности
        анкорной сцены и затем вызвать entity_scenes для связанных сцен.
        """
        return self.books._read(lambda tx: [row['id'] for row in tx.run('''
            MATCH (e:BookEntity {book_id:$id})-[:PRESENT_IN]->(s:NarrativeScene {book_id:$id})
            WHERE s.id IN $scenes
              AND ($types IS NULL OR e.entity_type IN $types)
            RETURN DISTINCT e.id AS id''',
            id=book_id, scenes=scene_ids, types=entity_types)])

    def linked_events(self, book_id, spans, scene_ids, link_types, ranges, limit):
        """TextChunk с событиями, связанными NARRATIVE_LINK (retells/parallels/causes)
        с событиями анкорных сцен/спанов.

        Анкор — NarrativeEvent, чей occurrence (локальные start_offset/end_offset
        внутри содержащей NarrativeScene, как у EntityMention) пересекает span
        кандидата или принадлежит анкорной сцене. Направление связи не различаем:
        обе стороны NARRATIVE_LINK интересны для поиска (это не решение о
        причинности, а кандидат для судьи).
        """
        return self.books._read(lambda tx: tx.run('''
            MATCH (s:NarrativeScene {book_id:$id})-[:HAS_EVENT]->(e:NarrativeEvent {book_id:$id})
            WHERE s.id IN $scene_ids
               OR any(span IN $spans WHERE s.start_char+e.start_offset<span.end
                                        AND s.start_char+e.end_offset>span.start)
            MATCH (e)-[link:NARRATIVE_LINK]-(other:NarrativeEvent {book_id:$id})
            WHERE link.type IN $types
            MATCH (os:NarrativeScene {book_id:$id})-[:HAS_EVENT]->(other)
            MATCH (os)-[:HAS_CHUNK]->(c:TextChunk {book_id:$id})
            WHERE c.start_char<=os.start_char+other.start_offset
              AND c.end_char>=os.start_char+other.end_offset
              AND any(r IN $ranges WHERE c.start_char<r.end AND c.end_char>r.start)
            RETURN DISTINCT c.id AS id, c.start_char AS start_char, c.end_char AS end_char,
                   link.type AS link_type, e.gist AS anchor_gist, other.gist AS target_gist,
                   link.reason AS reason, link.basis AS basis
            ORDER BY c.start_char, c.id LIMIT $limit''',
            id=book_id, spans=spans, scene_ids=scene_ids, types=list(link_types),
            ranges=ranges, limit=limit + 1).data())

    def linked_scenes(self, book_id, spans, scene_ids, link_types, ranges, limit):
        """Сцены, связанные SCENE_LINK (обзор сюжета) со сценами анкора. Связь,
        записанная для другой версии текста сцены (hash не совпадает), не читается."""
        return self.books._read(lambda tx: tx.run('''
            MATCH (s:NarrativeScene {book_id:$id})
            WHERE s.id IN $scene_ids
               OR any(span IN $spans WHERE s.start_char<span.end AND s.end_char>span.start)
            MATCH (s)-[link]-(o:NarrativeScene {book_id:$id})
            WHERE type(link) = 'SCENE_LINK' AND link.type IN $types
              AND startNode(link).text_hash = link.source_hash
              AND endNode(link).text_hash = link.target_hash
              AND any(r IN $ranges WHERE o.start_char<r.end AND o.end_char>r.start)
            RETURN DISTINCT o.id AS id, o.start_char AS start_char, o.end_char AS end_char,
                   link.type AS link_type, s.title AS anchor_title, o.title AS target_title,
                   link.reason AS reason, link.basis AS basis
            ORDER BY o.start_char, o.id LIMIT $limit''',
            id=book_id, spans=spans, scene_ids=scene_ids, types=list(link_types),
            ranges=ranges, limit=limit + 1).data())

    def similar_chunks(self, book_id, span, ranges, limit):
        """TextChunk, ближайшие по эмбеддингу к чанку, содержащему span (сам он и
        пересекающиеся с ним исключаются). score — как у semantic: (1 + cos) / 2."""
        return self.books._read(lambda tx: tx.run('''
            MATCH (c:TextChunk {book_id:$id})
            WHERE c.start_char<=$start AND c.end_char>=$end AND c.embedding IS NOT NULL
            WITH c ORDER BY c.end_char-c.start_char LIMIT 1
            MATCH (o:TextChunk {book_id:$id})
            WHERE o.embedding_model=c.embedding_model AND size(o.embedding)=size(c.embedding)
              AND NOT (o.start_char<c.end_char AND o.end_char>c.start_char)
              AND any(r IN $ranges WHERE o.start_char<r.end AND o.end_char>r.start)
            WITH o, vector.similarity.cosine(o.embedding,c.embedding) AS score
            RETURN o.id AS id, o.start_char AS start_char, o.end_char AS end_char, score
            ORDER BY score DESC, id LIMIT $limit''', id=book_id, start=span['start'],
            end=span['end'], ranges=ranges, limit=limit).data())

    def entities_possibly_same(self, book_id, entity_ids):
        """BookEntity, связанные POSSIBLY_SAME с заданными (не merge, неуверенное
        совпадение из book_resolution) — используется каналом soft_character
        поверх entity_scenes, без направления."""
        return self.books._read(lambda tx: [row['id'] for row in tx.run('''
            MATCH (a:BookEntity {book_id:$id})-[:POSSIBLY_SAME]-(b:BookEntity {book_id:$id})
            WHERE a.id IN $ids
            RETURN DISTINCT b.id AS id''', id=book_id, ids=entity_ids)])

    def chunk_context(self, book_id, spans):
        """Сцена-контейнер и до 200 символов хвоста/головы соседних чанков.

        Соседи ищутся через NEXT_CHUNK — сквозную цепочку по всей книге,
        независимую от границ сцены (см. replace_chunks). Кандидат без
        точного совпадающего TextChunk (устаревшая нарезка) не возвращается."""
        return self.books._read(lambda tx: tx.run('''
            UNWIND $spans AS span
            MATCH (c:TextChunk {book_id:$id})
            WHERE c.start_char<=span.start AND c.end_char>=span.end
            WITH span, c ORDER BY c.end_char-c.start_char ASC
            WITH span, collect(c)[0] AS c
            MATCH (b:LiteraryBook {id:$id})
            OPTIONAL MATCH (s:NarrativeScene {book_id:$id})-[:HAS_CHUNK]->(c)
            OPTIONAL MATCH (prev:TextChunk {book_id:$id})-[:NEXT_CHUNK]->(c)
            OPTIONAL MATCH (c)-[:NEXT_CHUNK]->(next:TextChunk {book_id:$id})
            RETURN span.id AS span_id, s.id AS scene_id, s.title AS scene_title,
                   s.summary AS scene_summary,
                   CASE WHEN prev IS NULL THEN '' ELSE substring(b.text,
                       CASE WHEN prev.end_char-prev.start_char<200 THEN prev.start_char
                            ELSE prev.end_char-200 END,
                       CASE WHEN prev.end_char-prev.start_char<200
                            THEN prev.end_char-prev.start_char ELSE 200 END) END AS prev_tail,
                   CASE WHEN next IS NULL THEN '' ELSE substring(b.text, next.start_char,
                       CASE WHEN next.end_char-next.start_char<200
                            THEN next.end_char-next.start_char ELSE 200 END) END AS next_head
            ''', id=book_id, spans=spans).data())

    def excerpts(self, book_id, rows, limit):
        return self.books._read(lambda tx: tx.run('''MATCH (b:LiteraryBook {id:$id})
            UNWIND $rows AS row
            RETURN row.id AS id, substring(b.text,row.start_char,
                CASE WHEN row.end_char-row.start_char<$limit THEN row.end_char-row.start_char ELSE $limit END) AS text''',
            id=book_id, rows=rows, limit=limit).data())
