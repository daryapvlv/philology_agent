"""SQLite-адаптер диалогов с одноразовым импортом старых JSON."""
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import re
import shutil
import sqlite3

from chat.model import Chat
from .database import connect, default_database_path, initialize


def _now():
    return datetime.now(timezone.utc).isoformat()


class SqliteChatRepository:
    def __init__(self, database_path, legacy_chats_dir=None):
        self.database_path = Path(database_path)
        self.legacy_chats_dir = Path(legacy_chats_dir) if legacy_chats_dir else None
        self._setup()
        self._import_legacy()

    def _connect(self):
        return connect(self.database_path)

    def _setup(self):
        initialize(self.database_path)

    @staticmethod
    def _validate_id(chat_id):
        if not isinstance(chat_id, str) or not re.fullmatch(r'[0-9a-f]{32}', chat_id):
            raise ValueError('Некорректный идентификатор чата')

    @staticmethod
    def _conversation_values(chat, now):
        return (chat.chat_id, chat.book_id, chat.pending_book_id, chat.title, now, now)

    def ensure(self, chat: Chat):
        """Создать чат один раз; уже существующую историю не перезаписывать."""
        self._validate_id(chat.chat_id)
        now = _now()
        with self._connect() as db:
            cursor = db.execute('''INSERT INTO conversations
                (id,book_id,pending_book_id,title,created_at,updated_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(id) DO NOTHING''', self._conversation_values(chat, now))
            if cursor.rowcount:
                db.executemany('''INSERT INTO messages
                    (conversation_id,position,role,content,tools) VALUES (?,?,?,?,?)''', [
                    (chat.chat_id, position, message.get('role', ''),
                     message.get('content', ''), message.get('tools', ''))
                    for position, message in enumerate(chat.messages)
                ])
        return chat

    def update(self, chat: Chat):
        """Обновить только метаданные чата, не затрагивая сообщения."""
        self._validate_id(chat.chat_id)
        with self._connect() as db:
            cursor = db.execute('''UPDATE conversations
                SET book_id=?, pending_book_id=?, title=?, updated_at=? WHERE id=?''',
                (chat.book_id, chat.pending_book_id, chat.title, _now(), chat.chat_id))
            if not cursor.rowcount:
                raise ValueError('Чат не найден')
        return chat

    def append_message(self, chat: Chat, message: dict):
        """Атомарно добавить одно сообщение и актуализировать метаданные чата."""
        self._validate_id(chat.chat_id)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute('''UPDATE conversations
                SET book_id=?, pending_book_id=?, title=?, updated_at=? WHERE id=?''',
                (chat.book_id, chat.pending_book_id, chat.title, _now(), chat.chat_id))
            if not cursor.rowcount:
                raise ValueError('Чат не найден')
            position = db.execute('''SELECT COALESCE(MAX(position), -1) + 1
                FROM messages WHERE conversation_id=?''', (chat.chat_id,)).fetchone()[0]
            db.execute('''INSERT INTO messages
                (conversation_id,position,role,content,tools) VALUES (?,?,?,?,?)''',
                (chat.chat_id, position, message.get('role', ''),
                 message.get('content', ''), message.get('tools', '')))
        return chat

    def get(self, chat_id):
        self._validate_id(chat_id)
        with self._connect() as db:
            row = db.execute('SELECT * FROM conversations WHERE id=?', (chat_id,)).fetchone()
            if row is None:
                raise ValueError('Чат не найден')
            messages = [dict(role=item['role'], content=item['content'], tools=item['tools'])
                        for item in db.execute('''SELECT role,content,tools FROM messages
                            WHERE conversation_id=? ORDER BY position''', (chat_id,))]
        return Chat(chat_id=row['id'], book_id=row['book_id'],
                    pending_book_id=row['pending_book_id'], title=row['title'], messages=messages)

    def list(self):
        with self._connect() as db:
            rows = db.execute('''SELECT id,title,book_id,updated_at FROM conversations
                                 ORDER BY updated_at DESC,id''').fetchall()
        return [dict(id=row['id'], title=row['title'],
                     book=row['book_id'][:12] if row['book_id'] else 'Без книги',
                     document_id=row['book_id'], updated=row['updated_at']) for row in rows]

    def delete(self, chat_id):
        self._validate_id(chat_id)
        with self._connect() as db:
            db.execute('DELETE FROM research_sessions WHERE research_id=?', (chat_id,))
            db.execute('DELETE FROM conversations WHERE id=?', (chat_id,))
        # Старые JSON и lock-файлы больше не читаются, но удаляются вместе с чатом.
        if self.legacy_chats_dir:
            directory = self.legacy_chats_dir / chat_id
            if directory.is_dir() and directory.parent == self.legacy_chats_dir:
                shutil.rmtree(directory)

    def _import_legacy(self):
        if not self.legacy_chats_dir or not self.legacy_chats_dir.exists():
            return
        for path in self.legacy_chats_dir.glob('*/chat.json'):
            try:
                self._validate_id(path.parent.name)
                with self._connect() as db:
                    exists = db.execute('SELECT 1 FROM conversations WHERE id=?',
                                        (path.parent.name,)).fetchone()
                if exists:
                    continue
                data = json.loads(path.read_text(encoding='utf-8'))
                chat = Chat(chat_id=path.parent.name, book_id=data.get('book_id', ''),
                            pending_book_id=data.get('pending_book_id', ''),
                            title=data.get('title', 'Новый чат'), messages=data.get('messages', []))
                self.ensure(chat)
            except (ValueError, OSError, KeyError, json.JSONDecodeError, sqlite3.Error):
                continue


@lru_cache(maxsize=1)
def get_chat_repository():
    root = Path(__file__).resolve().parent.parent.parent
    return SqliteChatRepository(default_database_path(), root / 'artifacts' / 'chats')
