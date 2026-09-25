"""Единая точка подключения и схема локальной SQLite."""
import os
from pathlib import Path
import sqlite3


SCHEMA = '''
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        book_id TEXT NOT NULL DEFAULT '',
        pending_book_id TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        tools TEXT NOT NULL DEFAULT '',
        UNIQUE(conversation_id, position)
    );
    CREATE INDEX IF NOT EXISTS conversations_updated
        ON conversations(updated_at DESC);
    CREATE TABLE IF NOT EXISTS research_sessions (
        id TEXT PRIMARY KEY,
        book_id TEXT NOT NULL,
        research_id TEXT NOT NULL,
        state_json TEXT NOT NULL,
        version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS research_sessions_owner
        ON research_sessions(research_id, book_id, updated_at DESC);
'''


def default_database_path():
    root = Path(__file__).resolve().parent.parent.parent
    return Path(os.getenv('PHILOLOGY_DB_PATH', root / 'artifacts' / 'philology.sqlite3'))


def connect(database_path):
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=10000')
    return connection


def initialize(database_path):
    with connect(database_path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)
