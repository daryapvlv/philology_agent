# Сущности книги

`entities` — производный семантический слой над готовыми `NarrativeScene`. Не
входит в структурный снимок книги и не меняет её ревизию.

```python
from entities import EntityService
from llm import configured_client

client, config = configured_client()
service = EntityService(book_id)
with client:
    service.extract_scene_mentions(scene_id, client, config=config)
    service.resolve_mentions(client, scene_id=scene_id, config=config)
    service.resolve_chapter(chapter_id, client, config=config)

service.resolve_book(client, config=config)          # между главами книги
service.reconcile_book(client, config=config)         # доп. проход поверх уже разрешённых
```

## Три уровня

1. **Extraction** — LLM вызывается отдельно по каждому `TextChunk` сцены (не
   одним запросом на всю сцену), координаты переводятся в `NarrativeScene.text`
   с проверкой точного совпадения `surface_text`. Даёт `EntityMention`
   (`HAS_MENTION`) и локальные `EntityReference` (`HAS_REFERENCE` +
   `REFERS_TO_MENTION` на anchor той же сцены).
2. **Chapter resolution** — группирует mentions главы в `ChapterEntity`
   (`HAS_CHAPTER_ENTITY`/`IN_CHAPTER_ENTITY`) батчами, затем отдельный проход
   объединяет только подтверждённые `SAME_ENTITY`-пары.
3. **Book resolution** — разрешает `ChapterEntity` между главами
   последовательно (глава за главой) в `BookEntity`; `reconcile_book` — более
   дешёвый повторный проход поверх уже разрешённых `BookEntity`, который
   исправляет пропущенные из-за порядка обработки объединения (например,
   уменьшительное имя персонажа, встреченное раньше остального контекста).
   Запуск из интерфейса делает полный rebuild: разрешает все актуальные
   `ChapterEntity`, затем одной транзакцией заменяет `BookEntity` и связи.

На обоих уровнях резолюции: неуверенная сущность остаётся `uncertain` и
разделённой, а не объединяется — false merge хуже false split. Кандидаты с
конфликтующим `entity_type` (например `person` и `vehicle`) исключаются из
shortlist жёстко, независимо от лексического совпадения имён.

`Entity` принадлежит одной книге (`book_id`, `HAS_ENTITY`): одинаковые имена в
разных книгах не объединяются. Изменение текста/границ сцены удаляет её
mentions; канонические сущности сохраняются.

## После resolution

Сервис детерминированно (без LLM) перестраивает `BookEntity-[:PRESENT_IN]->
NarrativeScene` из цепочки mention → ChapterEntity → BookEntity, с
`mention_count`/`reference_count`. `NEXT_SCENE` поддерживается независимо от
entity resolution (`structure.graph_db.repository.rebuild_next_scene_chain`).

Доступны: `rebuild_graph`, `get_scenes_for_entity`, `get_entities_for_scene`,
`get_next_scene`/`get_previous_scene`. В интерфейсе — вкладка «Подготовка»
(«Разрешить сущности книги» / «Сверить сущности заново»).
