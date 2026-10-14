"""Durable polling acceptance, before PTB advances Telegram's update offset.

SQLite commits are FULL-synchronous. Callers run all methods off their event
loops. Undelivered/unfinished rows have no age cutoff and survive adapter rebuilds.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import uuid


class UpdateInbox:
    def __init__(self, directory: Path, bot_id: str):
        if not bot_id.isdigit():
            raise ValueError("Telegram bot token must contain a numeric bot id")
        self.path = Path(directory) / f"telegram_update_inbox_{bot_id}.sqlite3"

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("CREATE TABLE IF NOT EXISTS updates (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                           "update_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, kind TEXT NOT NULL)")
        return connection

    def put(self, item):
        from telegram import Update
        if isinstance(item, Update):
            key, payload, kind = str(item.update_id), item.to_json(), "update"
        else:
            # PTB test doubles and non-user queue items keep their JSON shape.
            key, payload, kind = uuid.uuid4().hex, json.dumps(item), "json"
        with closing(self._connect()) as connection, connection:
            connection.execute("INSERT OR IGNORE INTO updates(update_id, payload, kind) VALUES (?, ?, ?)",
                               (key, payload, kind))
        # Returning means the transaction committed, never just queued a write.

    def after(self, sequence, limit=256):
        with closing(self._connect()) as connection:
            return connection.execute("SELECT seq, payload, kind FROM updates WHERE seq > ? "
                                      "ORDER BY seq LIMIT ?", (sequence, limit)).fetchall()

    def complete(self, update_ids):
        with closing(self._connect()) as connection, connection:
            connection.executemany("DELETE FROM updates WHERE update_id = ?", ((str(uid),) for uid in update_ids))

    def count(self):
        with closing(self._connect()) as connection:
            return connection.execute("SELECT count(*) FROM updates").fetchone()[0]
