import json
from types import SimpleNamespace
import unittest

from entities import BookEntityResolver, PersistentEntityExtractor
from entities.config import BookResolutionConfig
from entities.workflow import parse_mentions


class FakeClient:
    base_url = "test://entities"

    def __init__(self, reply):
        self.reply = reply
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    def with_options(self, **_):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        value = self.reply(kwargs) if callable(self.reply) else self.reply
        return SimpleNamespace(
            usage=SimpleNamespace(total_tokens=100),
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=json.dumps(value, ensure_ascii=False)),
            )],
        )


class LengthThenCompleteClient(FakeClient):
    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            finish_reason, content = "length", None
        else:
            finish_reason = "stop"
            content = json.dumps({"mentions": [{
                "local_id": "a1", "surface_text": "Анна",
                "start_offset": 0, "end_offset": 4,
                "entity_type": "person",
            }], "references": []}, ensure_ascii=False)
        return SimpleNamespace(
            usage=SimpleNamespace(total_tokens=kwargs.get("max_tokens", 100)),
            choices=[SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content),
            )],
        )


class EntityWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.scene = {
            "id": "scene-1", "text": "Анна встретила Вронского. Анна молчала.",
            "text_hash": "scene-hash",
        }

    def test_mentions_use_exact_scene_local_offsets(self):
        mentions = parse_mentions({"mentions": [
            {"surface_text": "Анна", "start_offset": 0, "end_offset": 4,
             "entity_type": "person", "extraction_confidence": 0.9},
            {"surface_text": "Вронского", "start_offset": 15, "end_offset": 24},
            {"surface_text": "Анна", "start_offset": 26, "end_offset": 30},
        ]}, book_id="book-a", scene=self.scene)
        self.assertEqual([item["surface_text"] for item in mentions],
                         ["Анна", "Вронского", "Анна"])
        self.assertEqual(
            [self.scene["text"][item["start_offset"]:item["end_offset"]]
             for item in mentions],
            [item["surface_text"] for item in mentions],
        )
        self.assertNotEqual(mentions[0]["id"], mentions[2]["id"])

    def test_extraction_retries_length_with_larger_completion_budget(self):
        scene = {"id": "scene-retry", "text": "Анна вошла.",
                 "text_hash": "hash"}
        client = LengthThenCompleteClient(None)
        result = PersistentEntityExtractor(client, cache_dir=None).extract(scene)
        self.assertEqual([row["surface_text"] for row in result["mentions"]], ["Анна"])
        self.assertEqual([call["max_tokens"] for call in client.calls], [12000, 24000])

class BookEntityResolverTests(unittest.TestCase):
    @staticmethod
    def chapter_entity(index, name, *, aliases=None, entity_type="person"):
        return {
            "id": f"chapter-entity-{index}", "book_id": "book-a",
            "chapter_id": f"chapter-{index}", "canonical_name": name,
            "entity_type": entity_type, "aliases": aliases or [name],
            "mention_ids": [f"mention-{index}"], "scene_ids": [f"scene-{index}"],
            "representative_mentions": [name],
            "representative_contexts": [f"Контекст главы {index}: {name}."],
        }

    def run_resolution(self, sources, reply, *, existing=None, batch_size=8,
                       apply_batch=None):
        config = BookResolutionConfig(batch_size=batch_size, top_k=3,
                                      model="gpt-4o-mini")
        return BookEntityResolver(FakeClient(reply), config=config, cache_dir=None).resolve(
            book_id="book-a", chapter_entities=sources,
            book_entities=existing or [], apply_batch=apply_batch,
        )

    def test_same_name_and_type_do_not_force_merge(self):
        source = self.chapter_entity(2, "Алексей")
        existing = [{"id": "book-entity-1", "book_id": "book-a",
                     "canonical_name": "Алексей", "entity_type": "person",
                     "aliases": ["Алексей"], "chapter_ids": ["chapter-1"],
                     "scene_ids": ["scene-1"],
                     "representative_contexts": ["Другой Алексей ответил."]}]
        result = self.run_resolution([source], {"resolutions": [{
            "chapter_entity_id": source["id"], "decision": "new_entity",
        }]}, existing=existing)
        self.assertEqual(len(result["entities"]), 2)

    def test_malformed_batch_reply_degrades_to_uncertain(self):
        # Строка ответа для сущности отсутствует или задублирована/противоречива —
        # оба случая безопасно деградируют в uncertain, а не падают и не гадают.
        missing = self.chapter_entity(1, "Москва", entity_type="location")
        extra = self.chapter_entity(2, "Москве", entity_type="location")
        missing_result = self.run_resolution([missing, extra], {"resolutions": [{
            "chapter_entity_id": missing["id"], "decision": "new_entity",
        }]})
        self.assertEqual([row["decision"] for row in missing_result["decisions"]],
                         ["new_entity", "uncertain"])

        duplicated = self.chapter_entity(1, "Москва", entity_type="location")
        duplicate_result = self.run_resolution([duplicated], {"resolutions": [
            {"chapter_entity_id": duplicated["id"], "decision": "new_entity"},
            {"chapter_entity_id": duplicated["id"], "decision": "uncertain"},
        ]})
        self.assertEqual(duplicate_result["decisions"][0]["decision"], "uncertain")

    def test_person_never_merges_with_conflicting_type_even_as_sole_candidate(self):
        # Регрессия на реальный баг: 'Марья Ивановна' (person) и 'башкирскую
        # лошадь' (vehicle) были ошибочно слиты в одну сущность внутри главы,
        # потому что кандидат ничем не отсекался по типу. Здесь единственная
        # existing сущность — конфликтующего типа, поэтому даже если бы LLM
        # (ошибочно) сказала same_entity, кандидат не должен был вообще дойти
        # до неё как вариант.
        source = self.chapter_entity(1, "Марья Ивановна", entity_type="person")
        existing = [{"id": "book-entity-horse", "book_id": "book-a",
                     "canonical_name": "башкирскую лошадь", "entity_type": "vehicle",
                     "aliases": ["башкирскую лошадь"], "chapter_ids": ["chapter-0"],
                     "scene_ids": ["scene-0"],
                     "representative_contexts": ["...башкирскую лошадь в поводья..."]}]

        def reply(kwargs):
            row = json.loads(kwargs["messages"][1]["content"])["chapter_entities"][0]
            self.assertEqual(row["candidates"], [])
            return {"resolutions": [{
                "chapter_entity_id": row["chapter_entity_id"],
                "decision": "same_entity", "entity_id": "book-entity-horse",
            }]}

        result = self.run_resolution([source], reply, existing=existing)
        self.assertEqual(result["decisions"][0]["decision"], "uncertain")
        self.assertIsNone(result["decisions"][0]["entity_id"])
        self.assertEqual([entity["canonical_name"] for entity in result["entities"]],
                         ["башкирскую лошадь"])

if __name__ == "__main__":
    unittest.main()
