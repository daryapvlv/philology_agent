"""SQLite-адаптер снимков retrieval research без изменения их формата."""
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3

from structure.graph_db.repository import ConflictError
from .database import connect, default_database_path, initialize


def _now():
    return datetime.now(timezone.utc).isoformat()


class SqliteSearchSessions:
    def __init__(self, database_path, book_id, research_id, legacy_directory=None):
        self.database_path = Path(database_path)
        self.book_id = book_id
        self.research_id = research_id
        self.legacy_directory = Path(legacy_directory) if legacy_directory else None
        self._setup()
        self._import_legacy()

    def _connect(self):
        return connect(self.database_path)

    def _setup(self):
        initialize(self.database_path)

    @staticmethod
    def _validate_id(search_id):
        if not isinstance(search_id, str) or not re.fullmatch(r'[0-9a-f]{32}', search_id):
            raise ValueError('Некорректный ID поиска')

    def _owned(self, db, search_id):
        row = db.execute('SELECT * FROM research_sessions WHERE id=?', (search_id,)).fetchone()
        if row is not None and (row['book_id'] != self.book_id or row['research_id'] != self.research_id):
            raise ValueError('Поиск не принадлежит этой книге и исследованию')
        return row

    def save(self, search_id, state, changed_ids=None):
        """Атомарно сохранить весь снимок с CAS по полю version."""
        self._validate_id(search_id)
        expected = state.get('version')
        now = _now()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self._owned(db, search_id)
            if ((old is None and expected is not None)
                    or (old is not None and old['version'] != expected)):
                raise ConflictError('Исследование изменилось; перечитай сессию и повтори действие')
            version = 0 if expected is None else expected + 1
            saved = dict(state, version=version)
            payload = json.dumps(saved, ensure_ascii=False, separators=(',', ':'))
            if old is None:
                db.execute('''INSERT INTO research_sessions
                    (id,book_id,research_id,state_json,version,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?)''',
                    (search_id, self.book_id, self.research_id, payload, version, now, now))
            else:
                db.execute('''UPDATE research_sessions SET state_json=?,version=?,updated_at=?
                              WHERE id=?''', (payload, version, now, search_id))
        state['version'] = version

    def load(self, search_id):
        self._validate_id(search_id)
        with self._connect() as db:
            row = self._owned(db, search_id)
        if row is None:
            raise ValueError('Поиск не найден в этом исследовании')
        return json.loads(row['state_json'])

    def recent(self, offset=0, limit=20, current=None):
        with self._connect() as db:
            rows = db.execute('''SELECT id,state_json,updated_at FROM research_sessions
                WHERE book_id=? AND research_id=? ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?''',
                (self.book_id, self.research_id, limit, offset)).fetchall()
        results = []
        for record in rows:
            state = json.loads(record['state_json'])
            items, cursor = state['items'], state.get('cursor', 0)
            results.append(dict(search_id=record['id'], query=state.get('query'),
                has_plan=bool(state.get('plan')),
                candidate_count=len(items), next_offset=cursor if cursor < len(items) else None,
                updated_at=record['updated_at'],
                current=current is not None and all(
                    current[key] == state['scope'][key] for key in ('revision', 'generation')),
                viewed=sum(bool(item.get('viewed')) for item in items),
                unreviewed=sum(item.get('review_status', 'unreviewed') == 'unreviewed' for item in items),
                rejected=sum(item.get('review_status') == 'rejected' for item in items),
                selected=sum(bool(item.get('selected')) for item in items)))
        return results

    def _import_legacy(self):
        if not self.legacy_directory or not self.legacy_directory.exists():
            return
        for path in self.legacy_directory.glob('*.json'):
            try:
                self._validate_id(path.stem)
                record = json.loads(path.read_text(encoding='utf-8'))
                if record.get('book_id') != self.book_id or record.get('research_id') != self.research_id:
                    continue
                state = record['state']
                version = state.get('version', 0)
                with self._connect() as db:
                    exists = db.execute('SELECT 1 FROM research_sessions WHERE id=?',
                                        (path.stem,)).fetchone()
                    if exists:
                        continue
                    db.execute('''INSERT INTO research_sessions
                        (id,book_id,research_id,state_json,version,created_at,updated_at)
                        VALUES (?,?,?,?,?,?,?)''', (
                        path.stem, self.book_id, self.research_id,
                        json.dumps(state, ensure_ascii=False, separators=(',', ':')), version,
                        record.get('created_at', _now()), record.get('updated_at', _now())))
            except (ValueError, OSError, KeyError, json.JSONDecodeError, sqlite3.Error):
                continue


def _legacy_directory(root, research_id):
    if re.fullmatch(r'[0-9a-f]{32}', research_id):
        return root / 'artifacts' / 'chats' / research_id / 'searches'
    key = sha256(research_id.encode()).hexdigest()
    return root / 'artifacts' / 'research' / key / 'searches'


@lru_cache(maxsize=128)
def get_search_sessions(book_id, research_id='default'):
    root = Path(__file__).resolve().parent.parent.parent
    return SqliteSearchSessions(default_database_path(), book_id, research_id,
                                _legacy_directory(root, research_id))
