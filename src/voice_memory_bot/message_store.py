from __future__ import annotations

import json
import time


class MessageStore:
    """Exact Telegram receipts and durable, explicitly requested move cleanup."""

    def __init__(self, store):
        self.store = store
        self.db = store.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS note_messages (
                owner INTEGER NOT NULL, message_id INTEGER NOT NULL,
                note_id TEXT NOT NULL REFERENCES notes(id), thread_id INTEGER,
                kind TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(owner, message_id)
            );
            CREATE INDEX IF NOT EXISTS note_message_lookup ON note_messages(note_id, thread_id);
            CREATE TABLE IF NOT EXISTS note_moves (
                note_id TEXT PRIMARY KEY REFERENCES notes(id), owner INTEGER NOT NULL,
                target_folder TEXT NOT NULL, source_thread INTEGER,
                source_ids TEXT NOT NULL, state TEXT NOT NULL, error TEXT, updated_at REAL NOT NULL
            );
        """)
        with self.db:
            # Old versions kept only these exact IDs. Never infer adjacent message IDs.
            for field, thread, kind in (("message_id", "source_thread", "source"),
                                        ("transcript_message_id", "source_thread", "transcript"),
                                        ("result_message_id", "result_thread", "result")):
                self.db.execute(f"""INSERT OR IGNORE INTO note_messages(owner,message_id,note_id,thread_id,kind)
                    SELECT owner,{field},id,{thread},? FROM notes WHERE {field}>0 AND deleted=0""", (kind,))
            self.db.execute("UPDATE note_moves SET state='unknown' WHERE state='publishing'")

    def record(self, note, message_id, thread, kind):
        if message_id <= 0:
            return
        self.store.note(note["owner"], note["id"])
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO note_messages(owner,message_id,note_id,thread_id,kind) "
                            "VALUES (?,?,?,?,?)", (note["owner"], message_id, note["id"], thread, kind))

    def in_thread(self, owner, identity, thread):
        self.store.note(owner, identity)
        return [row["message_id"] for row in self.store.all(
            "SELECT message_id FROM note_messages WHERE owner=? AND note_id=? AND thread_id IS ? "
            "AND deleted=0 ORDER BY message_id", (owner, identity, thread))]

    def begin(self, note, folder_id, thread):
        source_ids = self.in_thread(note["owner"], note["id"], thread)
        with self.db:
            self.db.execute("INSERT INTO note_moves VALUES (?,?,?,?,?,'publishing',NULL,?) "
                            "ON CONFLICT(note_id) DO UPDATE SET target_folder=excluded.target_folder, "
                            "source_thread=excluded.source_thread,source_ids=excluded.source_ids, "
                            "state='publishing',error=NULL,updated_at=excluded.updated_at",
                            (note["id"], note["owner"], folder_id, thread, json.dumps(source_ids), time.time()))
        return self.get(note["owner"], note["id"])

    def get(self, owner, identity):
        return self.store.one("SELECT * FROM note_moves WHERE owner=? AND note_id=?", (owner, identity))

    def state(self, owner, identity, state, error=None):
        with self.db:
            self.db.execute("UPDATE note_moves SET state=?,error=?,updated_at=? WHERE owner=? AND note_id=?",
                            (state, error, time.time(), owner, identity))

    def confirm(self, note, folder_id, response, thread):
        self.store.folder(note["owner"], folder_id)
        revision = ",revision=revision+1" if "revision" in note and folder_id != note["folder_id"] else ""
        with self.db:
            self.db.execute("UPDATE notes SET folder_id=?,routing='manual',requested_folder=NULL,"
                            "delivery='sent',result_message_id=?,result_thread=?" + revision +
                            " WHERE owner=? AND id=? AND deleted=0",
                            (folder_id, response["message_id"], thread, note["owner"], note["id"]))
            self.db.execute("UPDATE note_moves SET state='cleanup',updated_at=? WHERE owner=? AND note_id=?",
                            (time.time(), note["owner"], note["id"]))

    def removed(self, owner, identity, message_id):
        with self.db:
            self.db.execute("UPDATE note_messages SET deleted=1 WHERE owner=? AND note_id=? AND message_id=?",
                            (owner, identity, message_id))

    def pending(self):
        return self.store.one("SELECT m.* FROM note_moves m JOIN notes n ON n.id=m.note_id "
                              "WHERE m.state='cleanup' AND n.deleted=0 ORDER BY m.updated_at LIMIT 1")
