"""Durable Telegram video references, isolated by bot and exact file content."""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

CACHE_NAME = 'telegram-video-cache.sqlite3'


class VideoFileCache:
    def __init__(self, path: Path, bot_id: str):
        self.path, self.bot_id = Path(path), str(bot_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS videos ('
                       'bot_id TEXT NOT NULL, sha256 TEXT NOT NULL, file_id TEXT NOT NULL, '
                       'PRIMARY KEY(bot_id,sha256))')
        self.path.chmod(0o600)

    @staticmethod
    def digest(path):
        with Path(path).open('rb') as handle:
            return hashlib.file_digest(handle, 'sha256').hexdigest()

    def get(self, digest):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            row = db.execute('SELECT file_id FROM videos WHERE bot_id=? AND sha256=?',
                             (self.bot_id, digest)).fetchone()
        return row[0] if row else None

    def put(self, digest, file_id):
        if not isinstance(file_id, str) or not file_id:
            raise ValueError('telegram_video_file_id_missing')
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('INSERT INTO videos VALUES (?,?,?) ON CONFLICT(bot_id,sha256) '
                       'DO UPDATE SET file_id=excluded.file_id', (self.bot_id, digest, file_id))

    def discard(self, digest, file_id):
        # A concurrent preparation may already have replaced the rejected reference.
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('DELETE FROM videos WHERE bot_id=? AND sha256=? AND file_id=?',
                       (self.bot_id, digest, file_id))
