"""FULL-synchronous polling acceptance. All database work runs off event loops.

Limits pause new acceptance; they never expire or delete unfinished input.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
import uuid

_PRIVATE_CREATION = threading.RLock()


class UpdateInbox:
    def __init__(self, directory: Path, bot_id: str, *, max_updates=10000, max_age_seconds=21600):
        if not bot_id.isdigit():
            raise ValueError("Telegram bot token must contain a numeric bot id")
        self.bot_id = bot_id
        self.path = Path(directory) / f"telegram_update_inbox_{bot_id}.sqlite3"
        self.max_updates = max(1, int(max_updates))
        self.max_age_seconds = max(1, float(max_age_seconds))

    def _secure_files(self):
        if os.name == 'nt':
            return
        for suffix in ('', '-journal', '-wal', '-shm'):
            path = Path(str(self.path) + suffix)
            try:
                mode = os.lstat(path).st_mode
                if not stat.S_ISREG(mode):
                    raise OSError(f'Refusing non-regular Telegram inbox file: {path}')
                if stat.S_IMODE(mode) != 0o600:
                    os.chmod(path, 0o600)
            except FileNotFoundError:
                pass

    @contextmanager
    def _connection(self):
        # umask is process-wide: serialize inbox transactions and restore it on
        # every exit. Other threads can only get MORE restrictive creation
        # permissions during this window. Never hold this lock on an event loop.
        with _PRIVATE_CREATION:
            previous = os.umask(0o077) if os.name != 'nt' else None
            connection = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(fd)  # Never open/close an existing SQLite inode (fcntl locks).
                self._secure_files()
                connection = sqlite3.connect(self.path, timeout=30)
                connection.execute('PRAGMA synchronous=FULL')
                connection.execute('CREATE TABLE IF NOT EXISTS updates (seq INTEGER PRIMARY KEY AUTOINCREMENT, '
                                   'update_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, kind TEXT NOT NULL, '
                                   'received_at REAL NOT NULL)')
                columns = {row[1] for row in connection.execute('PRAGMA table_info(updates)')}
                if 'received_at' not in columns:
                    connection.execute('BEGIN IMMEDIATE')
                    connection.execute('ALTER TABLE updates ADD COLUMN received_at REAL NOT NULL DEFAULT 0')
                    # Legacy rows have no trustworthy acceptance timestamp. Give
                    # them one age window, retaining every payload.
                    connection.execute('UPDATE updates SET received_at = ?', (time.time(),))
                    connection.commit()
                connection.execute('CREATE INDEX IF NOT EXISTS updates_received_at ON updates(received_at)')
                yield connection
            finally:
                try:
                    if connection is not None:
                        connection.close()
                    self._secure_files()
                finally:
                    if previous is not None:
                        os.umask(previous)

    def put(self, item):
        from telegram import Update
        if isinstance(item, Update):
            key, payload, kind = str(item.update_id), item.to_json(), 'update'
        else:
            key, payload, kind = uuid.uuid4().hex, json.dumps(item), 'json'
        with self._connection() as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            # A redelivered prefix of a partial PTB batch is already durable;
            # accepting it again at capacity cannot grow the inbox.
            if connection.execute('SELECT 1 FROM updates WHERE update_id = ?', (key,)).fetchone():
                return True
            count, oldest = connection.execute('SELECT count(*), min(received_at) FROM updates').fetchone()
            now = time.time()
            if count >= self.max_updates or (oldest is not None and now - oldest >= self.max_age_seconds):
                return False
            connection.execute('INSERT INTO updates(update_id, payload, kind, received_at) VALUES (?, ?, ?, ?)',
                               (key, payload, kind, now))
        return True  # FULL commit completed before offset may advance.

    def after(self, sequence, limit=256):
        with self._connection() as connection:
            return connection.execute('SELECT seq, payload, kind FROM updates WHERE seq > ? '
                                      'ORDER BY seq LIMIT ?', (sequence, limit)).fetchall()

    def complete(self, update_ids):
        with self._connection() as connection, connection:
            cursor = connection.executemany('DELETE FROM updates WHERE update_id = ?', ((str(uid),) for uid in update_ids))
            return cursor.rowcount

    def count(self):
        with self._connection() as connection:
            return connection.execute('SELECT count(*) FROM updates').fetchone()[0]
