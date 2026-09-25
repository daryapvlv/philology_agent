"""Терминальный запуск того же агента, который используется в интерфейсе."""
import argparse

from application import AssistantService
from chat import Chat
from infrastructure.sqlite import get_chat_repository


def main():
    parser = argparse.ArgumentParser(description='Филологический LangGraph-агент')
    parser.add_argument('prompt')
    parser.add_argument('--book', default='')
    parser.add_argument('--chat', default='')
    args = parser.parse_args()
    chats = get_chat_repository()
    chat = chats.get(args.chat) if args.chat else Chat.create(args.book)
    if args.book:
        chat.attach(args.book)
    result = AssistantService(chats).send_message(chat, args.prompt)
    print(result.content)
    print(f'\nДиалог: {chat.chat_id}')


if __name__ == '__main__':
    main()
