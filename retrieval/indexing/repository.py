"""Подготовка и атомарное обновление поисковых данных книги."""
from uuid import uuid4

from structure.graph_db.repository import ConflictError
from retrieval.ranges import searchable_ranges


class IndexRepository:
    def __init__(self, books):
        self.books = books

    def source(self, book_id):
        def read(tx):
            row = tx.run('''MATCH (b:LiteraryBook {id:$id})
                RETURN b.text AS text, b.text_hash AS text_hash, b.revision AS revision,
                       b.search_generation AS generation''', id=book_id).single()
            if row is None:
                raise ValueError('Книга не найдена')
            sections = tx.run('''MATCH (s:BookSection {book_id:$id})
                OPTIONAL MATCH (s)-[:HAS_BLOCK]->(body:SectionBlock {role:'body'})
                RETURN s.id AS id,s.parent_id AS parent_id,
                       body.start_char AS body_start_char,body.end_char AS body_end_char,
                       [(s)-[:HAS_BLOCK]->(x:SectionBlock)
                        WHERE x.role IN ['epigraph','footnotes'] |
                        {start:x.start_char,end:x.end_char}] AS excluded_ranges''',
                id=book_id).data()
            cached = tx.run('''MATCH (c:TextChunk {book_id:$id})
                WHERE c.embedding IS NOT NULL AND c.embedding_hash IS NOT NULL
                RETURN c.embedding_hash AS hash, c.embedding AS embedding, c.embedding_model AS model''', id=book_id).data()
            contexts = tx.run('''MATCH (c:TextChunk {book_id:$id})
                WHERE c.narrative_context IS NOT NULL
                RETURN c.id AS id, c.narrative_context AS context''', id=book_id).data()
            scenes = tx.run('''MATCH (l:SceneLayer {book_id:$id})-[:HAS_SCENE]->
                    (s:NarrativeScene {book_id:$id})
                WHERE l.status IN ['ready','partial']
                  AND coalesce(s.description_stale,false)=false
                  AND trim(coalesce(s.title,'')+' '+coalesce(s.summary,''))<>''
                RETURN s.id AS id,s.chapter_id AS chapter_id,
                       s.start_char AS start_char,s.end_char AS end_char,
                       s.title AS title,s.summary AS summary,
                       coalesce(s.summary_embedding_hash,s.summary_hash) AS cached_hash,
                       s.summary_embedding AS cached_embedding,
                       s.summary_embedding_model AS cached_model
                ORDER BY s.start_char,s.id''', id=book_id).data()
            source = dict(row)
            source['body_ranges'] = searchable_ranges(sections)
            source['contexts'] = {r['id']: r['context'] for r in contexts}
            return source, cached, [dict(scene) for scene in scenes]
        return self.books._read(read)

    def replace_chunks(self, book_id, source, rows, scene_rows, model):
        generation = uuid4().hex
        def write(tx):
            row = tx.run('''MATCH (b:LiteraryBook {id:$id})
                SET b.search_lock=coalesce(b.search_lock,0)+1
                RETURN b.revision AS revision, b.text_hash AS text_hash,
                       b.search_generation AS generation''', id=book_id).single()
            if row is None or any(row[k] != source[k] for k in ('revision', 'text_hash', 'generation')):
                raise ConflictError('Книга или поисковый индекс изменились; повтори индексацию')
            tx.run('''MATCH (c:TextChunk {book_id:$id}) WHERE NOT c.id IN $ids
                DETACH DELETE c''', id=book_id, ids=[r['id'] for r in rows]).consume()
            tx.run('''UNWIND $rows AS row
                MERGE (c:TextChunk {book_id:$id,id:row.id}) SET c=row, c.book_id=$id
                WITH c MATCH (b:LiteraryBook {id:$id}) MERGE (b)-[:HAS_CHUNK]->(c)''',
                   id=book_id, rows=rows).consume()
            tx.run('''MATCH (:NarrativeScene {book_id:$id})-[r:HAS_CHUNK]->(:TextChunk)
                DELETE r''', id=book_id).consume()
            tx.run('''UNWIND $rows AS row
                MATCH (s:NarrativeScene {book_id:$id,id:row.scene_id}),
                      (c:TextChunk {book_id:$id,id:row.id})
                MERGE (s)-[:HAS_CHUNK]->(c)''', id=book_id, rows=rows).consume()
            tx.run('''MATCH (:TextChunk {book_id:$id})-[r:CONTAINS_MENTION]->()
                DELETE r''', id=book_id).consume()
            tx.run('''MATCH (s:NarrativeScene {book_id:$id})-[:HAS_CHUNK]->
                    (c:TextChunk {book_id:$id})
                MATCH (s)-[:HAS_MENTION]->(m:EntityMention {book_id:$id})
                WHERE c.start_char < s.start_char+m.end_offset
                  AND c.end_char > s.start_char+m.start_offset
                MERGE (c)-[:CONTAINS_MENTION]->(m)''', id=book_id).consume()
            tx.run('''MATCH (:TextChunk {book_id:$id})-[r:NEXT_CHUNK]->(:TextChunk)
                DELETE r''', id=book_id).consume()
            pairs = [[left['id'], right['id']] for left, right in zip(rows, rows[1:])]
            tx.run('''UNWIND $pairs AS pair
                MATCH (a:TextChunk {book_id:$id,id:pair[0]}),
                      (b:TextChunk {book_id:$id,id:pair[1]})
                MERGE (a)-[:NEXT_CHUNK]->(b)''', id=book_id, pairs=pairs).consume()
            tx.run('''MATCH (s:NarrativeScene {book_id:$id})
                REMOVE s.summary_embedding,s.summary_embedding_model,
                       s.summary_embedding_hash''', id=book_id).consume()
            tx.run('''UNWIND $rows AS row
                MATCH (s:NarrativeScene {book_id:$id,id:row.id})
                SET s.summary_embedding=row.embedding,
                    s.summary_embedding_model=row.embedding_model,
                    s.summary_embedding_hash=row.content_hash''',
                   id=book_id,
                   rows=[row for row in scene_rows if 'embedding' in row]).consume()
            vectors = ([r['embedding'] for r in rows if 'embedding' in r]
                       + [r['embedding'] for r in scene_rows if 'embedding' in r])
            tx.run('''MATCH (b:LiteraryBook {id:$id})
                SET b.search_generation=$generation, b.search_text_hash=$hash,
                    b.search_embedding_model=$model, b.search_chunk_count=$count,
                    b.search_embedded_count=$embedded, b.search_dimensions=$dimensions,
                    b.search_scene_count=$scene_count,
                    b.search_scene_embedded_count=$scene_embedded''',
                   id=book_id, generation=generation, hash=source['text_hash'], model=model,
                   count=len(rows), embedded=sum('embedding' in r for r in rows),
                   dimensions=len(vectors[0]) if vectors else None,
                   scene_count=len(scene_rows),
                   scene_embedded=sum('embedding' in r for r in scene_rows)).consume()
        self.books._write(write)
        return generation
