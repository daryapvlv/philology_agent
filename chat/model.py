"""Модель диалога без знания о способе хранения."""
from dataclasses import dataclass, field
from uuid import uuid4


WELCOME_MESSAGE = (
    "Книга подключена. Опишите, какие фрагменты найти: я подберу их по словам и смыслу, "
    "а выборку покажу таблицей во вкладке «Находки»."
)


@dataclass
class Chat:
    chat_id: str
    book_id: str = ''
    pending_book_id: str = ''
    title: str = 'Новый чат'
    messages: list[dict] = field(default_factory=list)

    @classmethod
    def create(cls, book_id: str = '') -> 'Chat':
        messages = [{'role': 'assistant', 'content': WELCOME_MESSAGE, 'tools': ''}] if book_id else []
        return cls(chat_id=uuid4().hex, book_id=book_id, messages=messages)

    def add(self, role: str, content: str, tools: str = '') -> None:
        self.messages.append({'role': role, 'content': content, 'tools': tools})
        if role == 'user' and self.title == 'Новый чат':
            self.title = content.strip()[:60] or 'Новый чат'

    def attach(self, book_id: str) -> None:
        if self.book_id and self.book_id != book_id:
            raise ValueError('В этом чате уже есть книга. Для другой книги создай новый чат.')
        self.book_id = book_id
        self.pending_book_id = ''
