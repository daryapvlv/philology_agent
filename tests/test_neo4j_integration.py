"""Тесты настоящего сервера. Создают и удаляют только книги с новым test-ID.

RUN_NEO4J_TESTS=1 python -m unittest discover -s tests -p test_neo4j_integration.py -v
Подключение берётся из NEO4J_*; приложение может использовать тот же сервер.
"""
from copy import deepcopy
import os
import shutil
import unittest
from uuid import uuid4

from neo4j import GraphDatabase
from test_structure import FakeClient, section
from ingest.repository import IngestRepository
from structure.graph_db.repository import BookRepository, ConflictError
from structure.rules import assemble_structure
from structure.application import BookService
from infrastructure.book_artifacts import book_dir, load_elements
from entities import EntityService
from entities.config import BookResolutionConfig


@unittest.skipUnless(os.getenv('RUN_NEO4J_TESTS') == '1', 'Нужен сервер Neo4j и RUN_NEO4J_TESTS=1')
class Neo4jIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = GraphDatabase.driver(os.environ['NEO4J_URI'], auth=(
            os.getenv('NEO4J_USERNAME', 'neo4j'), os.environ['NEO4J_PASSWORD']))
        cls.repo = BookRepository(cls.driver, os.getenv('NEO4J_DATABASE', 'neo4j'))
        cls.repo.setup()
        cls.ingest = IngestRepository(cls.repo)

    @classmethod
    def tearDownClass(cls):
        cls.driver.close()

    def setUp(self):
        self.book_id = 'test:' + uuid4().hex
        self.elements = [{'text': f'Строка {i}. Иван вернулся домой.'} for i in range(8)]
        sections = assemble_structure([section(0, 8, 1, 'work'), section(0, 4, 2, 'chapter'),
                                       section(4, 8, 2, 'chapter')], self.elements, self.book_id, [], [])
        self.result = dict(document_id=self.book_id, book_title='Тест Neo4j', source={'sha256': 'test'},
                           sections=sections, issues=[], rejected_sections=[], pending_review=True)
        self.ingest.import_book(self.result, self.elements)
        self.addCleanup(self.repo.delete, self.book_id)
        self.addCleanup(shutil.rmtree, book_dir(self.book_id), True)
        self.service = BookService(self.book_id, self.repo)
        self.chapter = sections[1]['id']

    def test_import_read_edit_rebuild_and_idempotency(self):
        self.ingest.import_book(self.result, self.elements)
        loaded, elements = self.repo.load(self.book_id)
        self.assertEqual(loaded['sections'], self.result['sections'])
        self.assertEqual(elements, self.elements)
        self.assertEqual(len(self.repo.outline(self.book_id)), 3)
        built = self.service.build_scenes(self.chapter, FakeClient(), cache_dir=None)
        other = built['sections'][2]['id']
        built = self.service.build_scenes(other, FakeClient(), cache_dir=None)
        sibling = deepcopy(built['scenes'][other])
        scene = built['scenes'][self.chapter]['items'][0]
        self.assertEqual(self.service.read_scene(self.chapter, scene['id'])['text'], '\n'.join(e['text'] for e in self.elements[:4]))
        self.assertEqual(len(self.service.search('вернулся', limit=3)), 3)
        edited = self.service.update_scene(self.chapter, scene['id'], 'split', built['revision'], boundary=20)
        self.assertEqual(len(self.service.scenes(self.chapter)['items']), 2)
        changed = self.service.update_section(self.chapter, {'end': 3}, edited['revision'])
        self.assertEqual(self.service.scenes(self.chapter)['status'], 'stale')
        rebuilt = self.service.build_scenes(self.chapter, FakeClient(), replace_human=True, cache_dir=None)
        self.assertEqual(rebuilt['scenes'][other], sibling)
        self.ingest.import_book(self.result, self.elements)
        after, _ = self.repo.load(self.book_id)
        self.assertEqual(after, rebuilt)
        with self.driver.session(database=self.repo.database) as session:
            count = session.run('MATCH (e:TextElement {book_id:$id}) RETURN count(e) AS count', id=self.book_id).single()['count']
            chapter = session.run('''MATCH (s:BookSection {book_id:$id,id:$chapter})
                RETURN s.start_char AS start, s.end_char AS end''',
                id=self.book_id, chapter=self.chapter).single()
            graph = session.run('''MATCH (chapter:BookSection {book_id:$id,id:$chapter})
                -[:HAS_BLOCK]->(body:SectionBlock {role:'body'})
                MATCH (body)-[:HAS_SCENE]->(scene:NarrativeScene)
                RETURN body.section_id AS owner, count(scene) AS scenes''',
                id=self.book_id, chapter=self.chapter).single()
            roots = session.run('''MATCH (:LiteraryBook {id:$id})-[:HAS_SECTION]->(root)
                RETURN count(root) AS count''', id=self.book_id).single()['count']
        self.assertEqual(count, 0)
        self.assertEqual(load_elements(self.book_id), self.elements)
        self.assertEqual((chapter['start'], chapter['end']),
                         (0, len('\n'.join(e['text'] for e in self.elements[:4]))))
        self.assertEqual((graph['owner'], graph['scenes']),
                         (self.chapter, len(rebuilt['scenes'][self.chapter]['items'])))
        self.assertEqual(roots, 1)

    def test_conflict_rolls_back_and_book_isolation(self):
        loaded, _ = self.repo.load(self.book_id)
        with self.assertRaises(ConflictError):
            self.repo.save(loaded, loaded['revision'] + 1)
        after, _ = self.repo.load(self.book_id)
        self.assertEqual(after, loaded)
        self.assertEqual(BookService('absent', self.repo).search('вернулся'), [])
        first = self.service.read_section(self.chapter, limit=10)
        second = self.service.read_section(self.chapter, offset=10, limit=10)
        self.assertEqual(first['end_char'], second['start_char'])

    def test_delete_leaves_no_owned_nodes(self):
        self.service.build_scenes(self.chapter, FakeClient(), cache_dir=None)
        self.repo.delete(self.book_id)
        self.assertFalse(self.repo.exists(self.book_id))
        with self.driver.session(database=self.repo.database) as session:
            count = session.run('MATCH (n {book_id:$id}) RETURN count(n) AS count', id=self.book_id).single()['count']
        self.assertEqual(count, 0)

    def test_entity_mentions_resolution_and_scene_invalidation(self):
        built = self.service.build_scenes(self.chapter, FakeClient(), cache_dir=None)
        scene = built['scenes'][self.chapter]['items'][0]
        entities = EntityService(self.book_id, self.repo)
        extracted = entities.extract_scene_mentions(
            scene['id'], FakeClient(lambda *_: {"mentions": [{
                "surface_text": "Иван", "start_offset": 10, "end_offset": 14,
                "entity_type": "person", "extraction_confidence": 0.8,
            }]}), cache_dir=None,
        )
        mention = extracted['mentions'][0]
        resolved = entities.resolve_mentions(
            FakeClient(lambda *_: {"resolutions": [{
                "mention_id": mention['id'], "new_entity": {
                    "canonical_name": "Герой", "entity_type": "person",
                    "aliases": ["Иван"],
                },
            }]}), scene_id=scene['id'], cache_dir=None,
        )
        self.assertEqual((resolved['resolved'], resolved['created_entities']), (1, 1))
        self.assertIsNotNone(entities.mentions(scene['id'])[0]['entity_id'])
        current, _ = self.service.snapshot()
        self.service.update_scene(self.chapter, scene['id'], 'split',
                                  current['revision'], boundary=20)
        self.assertEqual(entities.mentions(scene['id']), [])

    def test_book_entity_resolution_persists_links_and_is_idempotent(self):
        chapters = self.result['sections'][1:3]
        for chapter in chapters:
            self.service.build_scenes(chapter['id'], FakeClient(), cache_dir=None)
        with self.driver.session(database=self.repo.database) as session:
            scenes = session.run('''MATCH (l:SceneLayer {book_id:$book,status:'ready'})
                    -[:HAS_SCENE]->(s:NarrativeScene {book_id:$book})
                RETURN properties(s) AS scene ORDER BY s.start_char''',
                book=self.book_id).data()
        scenes = [dict(row['scene']) for row in scenes]
        rows = [{
            'id': f'chapter-entity-{index}', 'book_id': self.book_id,
            'chapter_id': chapter['id'], 'canonical_name': name,
            'entity_type': 'person', 'aliases': [name],
            'mention_ids': [f'mention-{index}'],
            'scene_ids': [scenes[index - 1]['id']], 'representative_mentions': [name],
            'representative_contexts': [f'{name} появился в главе.'],
        } for index, (chapter, name) in enumerate(zip(
            chapters, ['Анна', 'Анна Аркадьевна Каренина'], strict=True,
        ), 1)]
        with self.driver.session(database=self.repo.database) as session:
            session.run('''UNWIND $rows AS row
                MATCH (chapter:BookSection {book_id:$book,id:row.chapter_id})
                CREATE (c:ChapterEntity) SET c=row
                MERGE (chapter)-[:HAS_CHAPTER_ENTITY]->(c)''',
                book=self.book_id, rows=rows).consume()
            session.run('''UNWIND $rows AS row
                MATCH (s:NarrativeScene {book_id:$book,id:row.scene_id}),
                      (c:ChapterEntity {book_id:$book,id:row.chapter_entity_id})
                CREATE (m:EntityMention {book_id:$book,id:row.mention_id,
                    scene_id:row.scene_id,surface_text:row.surface_text,
                    start_offset:0,end_offset:size(row.surface_text)})
                MERGE (s)-[:HAS_MENTION]->(m)
                MERGE (m)-[:IN_CHAPTER_ENTITY]->(c)''', book=self.book_id,
                rows=[{'scene_id': scenes[index]['id'],
                       'chapter_entity_id': rows[index]['id'],
                       'mention_id': rows[index]['mention_ids'][0],
                       'surface_text': rows[index]['canonical_name']}
                      for index in range(2)]).consume()
            session.run('''MATCH (s:NarrativeScene {book_id:$book,id:$scene})
                    -[:HAS_MENTION]->(m:EntityMention {book_id:$book,id:$mention})
                CREATE (r:EntityReference {book_id:$book,id:'reference-1',
                    scene_id:$scene,surface_text:'героиня',start_offset:1,end_offset:8})
                MERGE (s)-[:HAS_REFERENCE]->(r)
                MERGE (r)-[:REFERS_TO_MENTION]->(m)''', book=self.book_id,
                scene=scenes[0]['id'], mention=rows[0]['mention_ids'][0]).consume()

        def reply(request, _call_number):
            payload = __import__('json').loads(request['messages'][1]['content'])
            resolutions = []
            for row in payload['chapter_entities']:
                candidates = row['candidates']
                resolutions.append({
                    'chapter_entity_id': row['chapter_entity_id'],
                    'decision': 'same_entity' if candidates else 'new_entity',
                    'entity_id': candidates[0]['entity_id'] if candidates else None,
                })
            return {'resolutions': resolutions}

        entities = EntityService(self.book_id, self.repo)
        client = FakeClient(reply)
        first = entities.resolve_book(
            client, config=BookResolutionConfig(batch_size=1), cache_dir=None,
        )
        second_client = FakeClient(reply)
        second = entities.resolve_book(
            second_client, config=BookResolutionConfig(batch_size=1), cache_dir=None,
        )
        self.assertEqual(len(first['entities']), 1)
        self.assertEqual(len(entities.book_entities()), 1)
        self.assertEqual(first['graph'], {
            'presence_edges': 2, 'next_edges': 1, 'scenes': 2,
        })
        self.assertEqual(second['usage']['calls'], 0)
        self.assertEqual(len(second_client.calls), 0)
        book_entity_id = first['entities'][0]['id']
        presence = entities.get_scenes_for_entity(book_entity_id)
        self.assertEqual([row['mention_count'] for row in presence], [1, 1])
        self.assertEqual([row['reference_count'] for row in presence], [1, 0])
        self.assertEqual(
            [row['id'] for row in entities.get_entities_for_scene(scenes[0]['id'])],
            [book_entity_id],
        )
        self.assertEqual(entities.get_next_scene(scenes[0]['id'])['id'], scenes[1]['id'])
        self.assertEqual(entities.get_previous_scene(scenes[1]['id'])['id'], scenes[0]['id'])
        self.assertIsNone(entities.get_previous_scene(scenes[0]['id']))
        self.assertEqual(entities.rebuild_graph(), first['graph'])
        with self.driver.session(database=self.repo.database) as session:
            links = session.run('''MATCH (:ChapterEntity {book_id:$book})
                -[:RESOLVES_TO]->(:BookEntity {book_id:$book})
                RETURN count(*) AS count''', book=self.book_id).single()['count']
        self.assertEqual(links, 2)


if __name__ == '__main__':
    unittest.main()
