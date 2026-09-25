# Retrieval: независимые стратегии поиска

Книгу и индексы модуль читает из Neo4j; состояние исследования — через
`SearchService`. Подготовка чанков/эмбеддингов — в [`retrieval.indexing`](indexing.md).
Поиск не строит сцены, не индексирует книгу и не вызывает генеративную модель —
какие стратегии запускать, решает план агента (`agent/research`); модуль даёт
независимые операции над сохранёнными поисками.

## Инструменты SearchService

| Метод | Действие |
|---|---|
| `search_words(query, target='text'\|'scenes', ...)` | Полнотекстовый поиск по отрывкам или названиям/описаниям сцен |
| `search_semantic(query, ...)` | RRF близости TextChunk и описаний их сцен |
| `find_entities(query=None, entity_types=None)` | Поиск `BookEntity` по имени/типу |
| `get_scenes_for_entities(entity_ids, mode=...)` | Intersection/union сцен сущностей |
| `get_neighbors(scene_id, before=, after=)` | Навигация по reading order |
| `expand_graph(...)` / `expand_narrative(..., channels=[...])` | Расширение от кандидатов/сцен по `NEXT_SCENE` или каналам discourse/composition/character/chronotope |
| `merge_results(search_ids, ...)` | Объединение сохранённых кандидатов нескольких поисков |
| `continue_search(search_id, ...)` | Продолжение с сохранённой позиции |
| `list_searches()` / `inspect_search(id)` | Сохранённые поиски, кандидаты, история решений |
| `review_candidate(search_id, candidate_id, status, reason)` | Решение о релевантности с причиной |
| `read_context(search_id, candidate_id, ...)` | Точный исходный текст с координатами |
| `apply_verdicts(search_id, verdicts)` | Записать статус и минимальную цитату по оценке |

Полный список аргументов и вспомогательных методов (`project_passages`,
`attach_plan`, `pending_candidates`, `context_of`, ...) — в докстрингах
`SearchService` ([retrieval/service.py](service.py)).

## Использование

```python
from retrieval import SearchService
from infrastructure.sqlite import get_search_sessions

search = SearchService(book_id, embedder=my_embedder, research_id=chat_id,
                       sessions=get_search_sessions(book_id, chat_id))

words = search.search_words('возвращение домой', section_ids=[chapter_id])
meaning = search.search_semantic('тревога перед встречей', section_ids=[chapter_id])
combined = search.merge_results([words['search_id'], meaning['search_id']])
fragment = search.read_context(combined['search_id'], combined['items'][0]['id'])
```

Область (`section_ids`) наследуется при расширении от кандидатов и не меняется
этим вызовом. `expand_graph` принимает `scene_ids` как независимый вход в граф
без текстового индекса и embedder'а.

### Каналы `expand_narrative`

| Канал | Что связывает | Нужен ли LLM |
|---|---|---|
| `discourse` | соседи по `NEXT_SCENE` | нет |
| `composition` | общее авторское членение (часть/акт) | нет |
| `character` | та же `person`-сущность | нужен entity-граф |
| `chronotope` | то же место действия | нужен entity-граф |

Без построенного entity-графа `character`/`chronotope` возвращают пустой
результат, а не ошибку (`index_status()['entity_graph_ready']`).

## Запуск из терминала

```sh
python -m retrieval.cli.index BOOK_ID
python -m retrieval words BOOK_ID 'страх возвращения домой'
python -m retrieval graph BOOK_ID --scene SCENE_ID --hops 1
```

## Ключевое поведение

- Лексический поиск — русский анализатор, слова через OR; не поиск точной цитаты.
- Семантический поиск сравнивает векторы только внутри выбранной области; без
  embedder/модели возвращает ошибку, не подменяя себя лексическим поиском.
- Результаты — кандидаты `relevance='unverified'`; `score` — сумма обратных
  рангов, не вероятность. Порядок выдачи — round-robin по каналам, не сортировка
  по score. `exhaustive=False`/`truncated_channels` не доказывают полноту.
- Состояние исследования (`research_id=chat_id`) хранится в
  `research_sessions` (`artifacts/philology.sqlite3`), с CAS-проверкой версии;
  текст книги там не копируется. Изменение книги/индекса помечает старый
  снимок устаревшим, но не удаляет историю.
- Решения (`review_candidate`/`apply_verdicts`) привязаны к конкретному
  поиску; при `merge_results` конфликтующие решения дают `uncertain`.

Подробнее о состоянии исследования, ранжировании и границах полноты — в коде
`retrieval/service.py` и тестах.

## Проверка

Синтаксис и SQLite-хранилище проверяются без Neo4j. Юнит-тесты —
`tests/test_hybrid_search.py`, `tests/test_retrieval_ranges.py`
(`python -m unittest discover -s tests`) — работают на фейковом репозитории и
не заменяют ручную проверку на реальном сервере (индекс → поиск известного
слова → расширение от сцены → объединение выдач → правка сцены делает старый
поиск недействительным).
