"""Оркестрация технических слоёв книги для главы или всей книги."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import os

from entities import EntityService
from narrative import NarrativeService, narrative_layer_enabled
from retrieval.indexing import IndexService
from structure.application import BookService


def describe_error(error):
    """Текст ошибки с исходной причиной: у сетевых ошибок SDK она в __cause__."""
    text = str(error) or type(error).__name__
    cause = error.__cause__
    if cause is not None and str(cause) and str(cause) not in text:
        text = f"{text} ({type(cause).__name__}: {cause})"
    return text


# Порядок стадий prepare() — ключи progress/PreparationError.key.
STAGE_KEYS = ('embeddings_check', 'scenes', 'chunks', 'mentions', 'chapter_entities',
              'book_entities', 'reconciliation', 'narrative', 'narrative_links')


class PreparationError(RuntimeError):
    """Сбой конкретного этапа; завершённые стадии уже сохранены в Neo4j.

    str() — техническая строка для логов; key/chapter_id/scene/detail — чтобы
    интерфейс собрал понятное сообщение, не разбирая строку."""

    def __init__(self, stage, error, *, key=None, chapter_id=None, scene=None):
        self.stage = stage
        self.key = key
        self.chapter_id = chapter_id
        self.scene = scene
        self.detail = describe_error(error)
        super().__init__(f"{stage}: {self.detail}")


class PreparationCancelled(RuntimeError):
    """Пользователь остановил подготовку между безопасными checkpoints."""


@contextmanager
def _stage(name, key, *, chapter_id=None, scene=None):
    try:
        yield
    except PreparationError:
        raise
    except Exception as error:
        raise PreparationError(name, error, key=key, chapter_id=chapter_id,
                               scene=scene) from error


class BookPreparationService:
    """Координатор существующих идемпотентных стадий, не UI-цикл."""

    def __init__(self, book_id, repository, client, config, *, embedder=None,
                 max_workers=None):
        self.book_id = book_id
        self.repository = repository
        self.client = client
        self.config = config
        self.embedder = embedder
        self.book = BookService(book_id, repository)
        self.entities = EntityService(book_id, repository)
        self.narrative = NarrativeService(book_id, repository)
        configured = max_workers if max_workers is not None else os.getenv(
            'BOOK_PREPARATION_WORKERS', '4')
        try:
            self.max_workers = int(configured)
        except (TypeError, ValueError) as error:
            raise ValueError('BOOK_PREPARATION_WORKERS должен быть числом от 1 до 8') from error
        if not 1 <= self.max_workers <= 8:
            raise ValueError('BOOK_PREPARATION_WORKERS должен быть числом от 1 до 8')

    @staticmethod
    def _failure(error, *, key, chapter_id, stage, scene=None):
        if (isinstance(error, PreparationError)
                and (error.chapter_id is not None or chapter_id is None)):
            return error
        if isinstance(error, PreparationError):
            error = RuntimeError(error.detail)
        return PreparationError(stage, error, key=key, chapter_id=chapter_id,
                                scene=scene)

    def _parallel(self, chapter_ids, action):
        """Выполнить независимую работу глав и вернуть result/error по id."""
        outcomes = {}
        workers = min(self.max_workers, len(chapter_ids))
        if not workers:
            return outcomes
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix='book-preparation') as pool:
            futures = {pool.submit(action, chapter_id): chapter_id
                       for chapter_id in chapter_ids}
            for future in as_completed(futures):
                chapter_id = futures[future]
                try:
                    outcomes[chapter_id] = (future.result(), None)
                except Exception as error:
                    outcomes[chapter_id] = (None, error)
        return {chapter_id: outcomes[chapter_id] for chapter_id in chapter_ids}

    def prepare(self, chapter_ids, *, with_embeddings=False, full_rebuild=False,
                reconcile=False, progress=None, continue_on_error=False,
                cancelled=None):
        """Подготовить главы.

        continue_on_error=True — книжный/UI-режим: ошибки глав входят в отчёт,
        остальные главы продолжаются. False сохранён для прямых вызовов API,
        которым нужен exception после завершения независимой работы.
        """
        chapter_ids = list(dict.fromkeys(chapter_ids))
        if not chapter_ids or not all(isinstance(value, str) and value for value in chapter_ids):
            raise ValueError('Нужна хотя бы одна глава для подготовки')
        known = {section['id'] for section in self.book.outline()['sections']}
        unknown = [chapter_id for chapter_id in chapter_ids if chapter_id not in known]
        if unknown:
            raise ValueError(f'Разделы отсутствуют в книге: {unknown}')
        # Разделы без основного текста (только примечания/эпиграф) не индексируются:
        # пропускаем их явно, а не падаем на «В главе нет текста» посреди книги.
        skipped = self.book.sections_without_scene_text(chapter_ids)
        chapter_ids = [chapter_id for chapter_id in chapter_ids if chapter_id not in skipped]
        if not chapter_ids:
            raise ValueError('В выбранных разделах нет основного текста для подготовки '
                             '(только примечания или эпиграф)')
        if with_embeddings and self.embedder is None:
            raise ValueError('Для embeddings настрой EMBEDDING_MODEL и ключ провайдера')
        # progress(key, position, total, chapter_id) — key из STAGE_KEYS; chapter_id
        # None для стадий уровня книги.
        report = progress or (lambda *_: None)
        is_cancelled = cancelled or (lambda: False)

        def check_cancelled():
            if is_cancelled():
                raise PreparationCancelled('Подготовка отменена пользователем')

        check_cancelled()

        if with_embeddings:
            # Недоступный провайдер эмбеддингов иначе обнаружился бы только внутри
            # построения сцен — после LLM-шага и с откатом уже разобранных границ.
            report('embeddings_check', 1, 1, None)
            with _stage('Эмбеддинги: проверка провайдера', 'embeddings_check'):
                try:
                    self.embedder.embed_query('проверка')
                except Exception as error:
                    raise ValueError(
                        f'Провайдер эмбеддингов недоступен ({describe_error(error)}). '
                        'Проверь EMBEDDING_BASE_URL и сеть или отключи смысловой поиск'
                    ) from error
            check_cancelled()

        positions = {chapter_id: position
                     for position, chapter_id in enumerate(chapter_ids, 1)}
        states = {chapter_id: {
            'chapter_id': chapter_id, 'state': 'pending', 'stages': {},
            'error': None,
        } for chapter_id in chapter_ids}

        def fail(chapter_id, error, *, key, stage, scene=None):
            failure = self._failure(error, key=key, chapter_id=chapter_id,
                                    stage=stage, scene=scene)
            states[chapter_id]['state'] = 'failed'
            states[chapter_id]['stages'][key] = 'failed'
            states[chapter_id]['error'] = failure

        def build_chapter(chapter_id):
            check_cancelled()
            position = positions[chapter_id]
            report('scenes', position, len(chapter_ids), chapter_id)
            with _stage(f'NarrativeScene, раздел {chapter_id}', 'scenes',
                        chapter_id=chapter_id):
                result = self.book.build_scenes(
                    chapter_id, self.client, config=self.config.for_stage('scenes'),
                    embedder=self.embedder if with_embeddings else None,
                )
                layer = result.get('scenes', {}).get(chapter_id, {})
                if layer.get('status') != 'ready':
                    issues = '; '.join(layer.get('issues') or []) or 'причина не записана'
                    raise ValueError(
                        f"сцены не готовы ({layer.get('status', 'not_started')}): {issues}"
                    )
            return layer

        layers = {}
        for chapter_id, (layer, error) in self._parallel(
                chapter_ids, build_chapter).items():
            if isinstance(error, PreparationCancelled):
                continue
            if error is not None:
                fail(chapter_id, error, key='scenes',
                     stage=f'NarrativeScene, раздел {chapter_id}')
            else:
                layers[chapter_id] = layer
                states[chapter_id]['stages']['scenes'] = 'done'

        check_cancelled()

        ready = [chapter_id for chapter_id in chapter_ids if chapter_id in layers]
        index = {'chunks': 0, 'chunks_with_embeddings': 0}
        if ready:
            check_cancelled()
            report('chunks', 1, 1, None)
            try:
                with _stage('TextChunk + embeddings', 'chunks'):
                    index = IndexService(
                        self.book_id, repository=self.repository,
                        embedder=self.embedder if with_embeddings else None,
                    ).index_book(chapter_ids=ready)
            except PreparationError as error:
                for chapter_id in ready:
                    fail(chapter_id, error, key='chunks',
                         stage='TextChunk + embeddings')
                ready = []
            else:
                for chapter_id in ready:
                    states[chapter_id]['stages']['chunks'] = 'done'
            check_cancelled()

        def extract_and_resolve(chapter_id):
            check_cancelled()
            position = positions[chapter_id]
            scenes = layers[chapter_id].get('items') or []
            for scene_position, scene in enumerate(scenes, 1):
                check_cancelled()
                report('mentions', scene_position, len(scenes), chapter_id)
                if (scene.get('entity_extraction_status') == 'ready'
                        and scene.get('entity_extraction_hash') == scene.get('text_hash')):
                    continue
                with _stage(f'EntityMention, раздел {chapter_id}, '
                            f'сцена {scene_position}/{len(scenes)}', 'mentions',
                            chapter_id=chapter_id, scene=f'{scene_position}/{len(scenes)}'):
                    self.entities.extract_scene_mentions(
                        scene['id'], self.client, config=self.config,
                    )
                check_cancelled()
            states[chapter_id]['stages']['mentions'] = 'done'
            report('chapter_entities', position, len(chapter_ids), chapter_id)
            with _stage(f'ChapterEntity, раздел {chapter_id}', 'chapter_entities',
                        chapter_id=chapter_id):
                result = self.entities.resolve_chapter(
                    chapter_id, self.client, config=self.config)
            return result

        chapter_results = []
        for chapter_id, (result, error) in self._parallel(
                ready, extract_and_resolve).items():
            if isinstance(error, PreparationCancelled):
                continue
            if error is not None:
                key = getattr(error, 'key', None) or 'chapter_entities'
                fail(chapter_id, error, key=key,
                     stage=f'Подготовка раздела {chapter_id}')
            else:
                chapter_results.append(result)
                states[chapter_id]['stages']['mentions'] = 'done'
                states[chapter_id]['stages']['chapter_entities'] = 'done'
                states[chapter_id]['state'] = 'done'

        check_cancelled()

        completed = [chapter_id for chapter_id in chapter_ids
                     if states[chapter_id]['state'] == 'done']

        book_result = {'entities': [], 'graph': {'presence_edges': 0, 'next_edges': 0}}
        global_errors = []
        if completed:
            check_cancelled()
            report('book_entities', 1, 1, None)
            try:
                with _stage('BookEntity', 'book_entities'):
                    book_result = self.entities.resolve_book(
                        self.client, config=self.config, rebuild=full_rebuild,
                    )
            except PreparationError as error:
                global_errors.append(error)
            check_cancelled()
        reconciliation = None
        if reconcile and completed and not global_errors:
            check_cancelled()
            report('reconciliation', 1, 1, None)
            with _stage('BookEntity reconciliation', 'reconciliation'):
                reconciliation = self.entities.reconcile_book(self.client, config=self.config)
            check_cancelled()

        narrative_results = []
        link_result = None
        if narrative_layer_enabled() and completed and not global_errors:
            def annotate_chapter(chapter_id):
                check_cancelled()
                results = []
                scenes = layers[chapter_id].get('items') or []
                for scene_position, scene in enumerate(scenes, 1):
                    check_cancelled()
                    report('narrative', scene_position, len(scenes), chapter_id)
                    with _stage(f'NarrativeEvent, раздел {chapter_id}, '
                                f'сцена {scene_position}/{len(scenes)}', 'narrative',
                                chapter_id=chapter_id, scene=f'{scene_position}/{len(scenes)}'):
                        results.append(
                            self.narrative.annotate_scene(scene['id'], self.client,
                                                          config=self.config)
                        )
                return results

            narrative_ready = []
            for chapter_id, (results, error) in self._parallel(
                    completed, annotate_chapter).items():
                if isinstance(error, PreparationCancelled):
                    continue
                if error is not None:
                    fail(chapter_id, error, key='narrative',
                         stage=f'NarrativeEvent, раздел {chapter_id}')
                else:
                    narrative_results.extend(results)
                    states[chapter_id]['stages']['narrative'] = 'done'
                    narrative_ready.append(chapter_id)
            check_cancelled()
            completed = narrative_ready
            if completed:
                report('narrative_links', 1, 1, None)
                try:
                    with _stage('NarrativeLink', 'narrative_links'):
                        link_result = self.narrative.rebuild_links(
                            self.client, config=self.config,
                            embedder=self.embedder if with_embeddings else None,
                        )
                except PreparationError as error:
                    global_errors.append(error)
                check_cancelled()

        failures = [state['error'] for state in states.values() if state['error']]

        if not continue_on_error:
            if failures:
                raise failures[0]
            if global_errors:
                raise global_errors[0]

        return {
            'book_id': self.book_id,
            'skipped': skipped,
            'chapters': chapter_results,
            'book_entities': book_result,
            'reconciliation': reconciliation,
            'narrative': narrative_results,
            'narrative_links': link_result,
            'index': index,
            'chapter_statuses': [states[chapter_id] for chapter_id in chapter_ids],
            'failed_chapter_ids': [state['chapter_id'] for state in states.values()
                                   if state['state'] == 'failed'],
            'completed_chapter_ids': completed,
            'failures': failures,
            'global_errors': global_errors,
        }
