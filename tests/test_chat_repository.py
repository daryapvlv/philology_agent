from pathlib import Path
import tempfile
import unittest

from chat import Chat
from infrastructure.sqlite import SqliteChatRepository


class ChatRepositoryTests(unittest.TestCase):
    def test_chat_roundtrip_and_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SqliteChatRepository(Path(directory) / 'app.sqlite3')
            chat = Chat.create('book-1')
            repository.ensure(chat)
            chat.add('user', 'Привет')
            repository.append_message(chat, chat.messages[-1])
            chat.add('assistant', 'Ответ', tools='search_words')
            repository.append_message(chat, chat.messages[-1])
            restored = repository.get(chat.chat_id)
            self.assertEqual(restored.book_id, 'book-1')
            self.assertEqual(restored.title, 'Привет')
            self.assertEqual(restored.messages, chat.messages)
            self.assertEqual(repository.list()[0]['document_id'], 'book-1')

if __name__ == '__main__':
    unittest.main()
