"""Один полный ход диалога: сохранение, сборка окружения и запуск агента."""
import logging

from agent import AgentRequest, AgentResult, PhilologyAgent, Workspace
from llm import configured_client
from infrastructure.sqlite import get_search_sessions
from retrieval import SearchService
from retrieval.embeddings import configured_embedder
from structure.application import BookService
from structure.graph_db.repository import get_repository

logger = logging.getLogger(__name__)


def build_workspace(*, chat_id, book_id, repository, sessions, embedder=None):
    if not book_id:
        return Workspace()
    search = SearchService(book_id, repository=repository,
                           embedder=embedder if embedder is not None else configured_embedder(),
                           research_id=chat_id or 'default', sessions=sessions)
    return Workspace(search=search, book=BookService(book_id, repository))


class AssistantService:
    def __init__(self, chats, *, client_factory=configured_client,
                 books_factory=get_repository, workspace_builder=build_workspace,
                 search_sessions_factory=get_search_sessions, agent_type=PhilologyAgent):
        self.chats = chats
        self.client_factory = client_factory
        self.books_factory = books_factory
        self.workspace_builder = workspace_builder
        self.search_sessions_factory = search_sessions_factory
        self.agent_type = agent_type

    @staticmethod
    def _metadata(chat, books=None):
        metadata = dict(book_id=chat.book_id, title='', processing=bool(chat.pending_book_id),
                        pending_review=False)
        if chat.book_id and books is not None:
            service = BookService(chat.book_id, books)
            result = service._structure()
            metadata.update(title=result['book_title'],
                            pending_review=result.get('pending_review', True))
        return metadata

    def send_message(self, chat, text, progress=None):
        text = text.strip()
        if not text:
            raise ValueError('Нужно непустое сообщение')
        # Новый чат может уже содержать приветствие; сохраняем его только при создании.
        self.chats.ensure(chat)
        chat.add('user', text)
        self.chats.append_message(chat, chat.messages[-1])

        try:
            if not self.agent_type.llm_enabled():
                result = self.agent_type().run(AgentRequest(messages=chat.messages), Workspace(),
                                               progress)
            else:
                books = self.books_factory(initialize=False) if chat.book_id else None
                client, config = self.client_factory()
                with client:
                    sessions = (self.search_sessions_factory(chat.book_id, chat.chat_id)
                                if chat.book_id else None)
                    workspace = self.workspace_builder(
                        chat_id=chat.chat_id, book_id=chat.book_id, repository=books,
                        sessions=sessions)
                    request = AgentRequest(messages=list(chat.messages),
                                           metadata=self._metadata(chat, books))
                    result = self.agent_type(client, config).run(request, workspace, progress)
        except Exception as error:
            logger.warning('Assistant turn failed: %s', type(error).__name__)
            detail = str(error)[:1000] if isinstance(error, ValueError) else type(error).__name__
            result = AgentResult(content=f'Не удалось завершить ответ ({detail}).')

        trace = ' → '.join(row['tool'] + (' (ошибка)' if row['status'] == 'error' else '')
                           for row in result.trace)
        chat.add('assistant', result.content, tools=trace)
        self.chats.append_message(chat, chat.messages[-1])
        return result
