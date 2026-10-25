"""Rehearse schema changes on a private SQLite backup, preserving existing rows."""
import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from voice_memory_bot.config import Limits
from voice_memory_bot.store import Store


def rehearse(database):
    with tempfile.TemporaryDirectory(prefix='voice-memory-migration-') as directory:
        path = Path(directory) / 'archive.sqlite3'
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as live:
            with closing(sqlite3.connect(path)) as copy:
                live.backup(copy)
        tables = ('notes', 'folders', 'users', 'updates', 'ai_requests', 'ai_steps', 'note_revisions', 'ai_note_context')
        with closing(sqlite3.connect(path)) as db:
            columns = {table: [r[1] for r in db.execute('PRAGMA table_info(' + table + ')')] for table in tables}
            queries = {table: 'SELECT ' + ','.join(names) + ' FROM ' + table + ' ORDER BY rowid'
                       for table, names in columns.items() if names}
            before = {table: hashlib.sha256(json.dumps(db.execute(query).fetchall()).encode()).hexdigest()
                      for table, query in queries.items()}
        store = Store(path, Limits())
        try:
            after = {table: hashlib.sha256(json.dumps([tuple(r) for r in store.db.execute(query)]).encode()).hexdigest()
                     for table, query in queries.items()}
            if before != after:
                raise RuntimeError('migration_changed_existing_archive_or_queue')
            if store.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('migration_integrity_failed')
            if store.db.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('migration_foreign_key_failure')
            return {'migration_rehearsal': 'ok', 'existing_rows_preserved': True,
                    'schema_version': store.db.execute('PRAGMA user_version').fetchone()[0]}
        finally:
            store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(rehearse(args.database)), flush=True)
