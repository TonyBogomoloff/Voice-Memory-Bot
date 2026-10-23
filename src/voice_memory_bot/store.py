from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
import uuid
from pathlib import Path

from .ai_store import migrate_ai
from .cleanup import migrate_cleanup
from .config import Limits


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().replace("ё", "е").split())


def clean_name(value: str) -> str:
    value = " ".join(value.split()).strip()
    if not 1 <= len(value) <= 80 or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("Название должно содержать от 1 до 80 символов")
    return value


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class Store:
    def __init__(self, path: Path, limits: Limits):
        self.limits = limits
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA secure_delete=ON;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, active_folder TEXT, action TEXT
            );
            CREATE TABLE IF NOT EXISTS folders (
                id TEXT PRIMARY KEY, owner INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL, normalized TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]',
                thread_id INTEGER, is_inbox INTEGER NOT NULL DEFAULT 0,
                UNIQUE(owner, normalized), UNIQUE(owner, thread_id)
            );
            CREATE TABLE IF NOT EXISTS user_export_settings (
                owner INTEGER PRIMARY KEY REFERENCES users(id),
                text_format TEXT NOT NULL DEFAULT 'pdf' CHECK(text_format IN ('pdf', 'markdown'))
            );
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY, owner INTEGER NOT NULL REFERENCES users(id),
                folder_id TEXT NOT NULL REFERENCES folders(id), source_folder TEXT NOT NULL,
                message_id INTEGER NOT NULL, source_thread INTEGER,
                kind TEXT NOT NULL, file_id TEXT, extension TEXT NOT NULL,
                declared_bytes INTEGER NOT NULL DEFAULT 0, duration INTEGER NOT NULL DEFAULT 0,
                audio_bytes INTEGER NOT NULL DEFAULT 0, sha256 TEXT,
                created_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                transcript TEXT, title TEXT NOT NULL DEFAULT 'Голосовая заметка',
                routing TEXT NOT NULL DEFAULT 'pending', requested_folder TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
                delivery TEXT NOT NULL DEFAULT 'pending', result_message_id INTEGER,
                result_thread INTEGER, deleted INTEGER NOT NULL DEFAULT 0,
                UNIQUE(owner, message_id)
            );
            CREATE INDEX IF NOT EXISTS note_folder ON notes(owner, folder_id, created_at);
            CREATE TABLE IF NOT EXISTS updates (
                id INTEGER PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS implicit_topics (
                owner INTEGER NOT NULL, thread_id INTEGER NOT NULL, PRIMARY KEY(owner, thread_id)
            );
            CREATE TABLE IF NOT EXISTS asr_usage (
                owner INTEGER NOT NULL, at REAL NOT NULL, seconds INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS usage_time ON asr_usage(owner, at);
        """)
        # Add checkpoints without replaying results already delivered by older versions.
        with self.db:
            self.db.execute("BEGIN")
            columns = {row["name"] for row in self.all("PRAGMA table_info(notes)")}
            if "status_message_id" not in columns:
                self.db.execute("ALTER TABLE notes ADD COLUMN status_message_id INTEGER")
            if "transcript_delivery" not in columns:
                self.db.execute("ALTER TABLE notes ADD COLUMN transcript_delivery TEXT NOT NULL DEFAULT 'pending'")
                self.db.execute("UPDATE notes SET transcript_delivery=delivery "
                                "WHERE delivery!='pending' AND transcript IS NOT NULL")
            if "transcript_message_id" not in columns:
                self.db.execute("ALTER TABLE notes ADD COLUMN transcript_message_id INTEGER")
            self.db.execute("UPDATE notes SET state='queued' WHERE state='processing' AND deleted=0")
            self.db.execute("UPDATE notes SET delivery='unknown' WHERE delivery='sending'")
            self.db.execute("UPDATE notes SET transcript_delivery='unknown' WHERE transcript_delivery='sending'")
            self.db.execute("UPDATE updates SET state='pending' WHERE state='processing'")
        migrate_ai(self.db)
        migrate_cleanup(self.db)

    def close(self):
        self.db.close()

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]

    def ensure_user(self, owner: int) -> str:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (owner,))
            folder = self.one("SELECT id FROM folders WHERE owner=? AND is_inbox=1", (owner,))
            if folder:
                return folder["id"]
            identity = new_id()
            self.db.execute("INSERT INTO folders(id,owner,name,normalized,is_inbox) VALUES (?,?,?,?,1)",
                            (identity, owner, "Входящие", normalize("Входящие")))
            return identity

    def folders(self, owner: int):
        return self.all("SELECT f.*, (SELECT count(*) FROM notes n WHERE n.folder_id=f.id "
                        "AND n.deleted=0 AND n.entry_type='note') AS count FROM folders f WHERE owner=? "
                        "ORDER BY is_inbox DESC, normalized", (owner,))

    def export_format(self, owner: int) -> str:
        row = self.one("SELECT text_format FROM user_export_settings WHERE owner=?", (owner,))
        return row["text_format"] if row else "pdf"

    def set_export_format(self, owner: int, text_format: str):
        if text_format not in {"pdf", "markdown"}:
            raise ValueError("Выбери PDF или Markdown")
        self.ensure_user(owner)
        with self.db:
            self.db.execute("INSERT INTO user_export_settings(owner,text_format) VALUES (?,?) "
                            "ON CONFLICT(owner) DO UPDATE SET text_format=excluded.text_format",
                            (owner, text_format))

    def folder(self, owner: int, identity: str):
        row = self.one("SELECT * FROM folders WHERE id=? AND owner=?", (identity, owner))
        if not row:
            raise ValueError("Папка не найдена")
        return row

    def folder_for_thread(self, owner: int, thread: int | None):
        if thread:
            return self.one("SELECT * FROM folders WHERE owner=? AND thread_id=?", (owner, thread))
        return None

    def mark_implicit_topic(self, owner, thread):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO implicit_topics VALUES (?,?)", (owner, thread))

    def is_implicit_topic(self, owner, thread):
        return bool(self.one("SELECT 1 FROM implicit_topics WHERE owner=? AND thread_id=?", (owner, thread)))

    def current_folder(self, owner: int, thread: int | None):
        inbox = self.ensure_user(owner)
        native = self.folder_for_thread(owner, thread)
        if native:
            return native["id"]
        active = self.one("SELECT active_folder FROM users WHERE id=?", (owner,))["active_folder"]
        return active or inbox

    def create_folder(self, owner: int, name: str, thread: int | None = None):
        self.ensure_user(owner)
        name = clean_name(name)
        self.check_names(owner, [name])
        if len(self.folders(owner)) >= self.limits.max_folders_per_user:
            raise ValueError("Достигнут лимит папок")
        identity = new_id()
        with self.db:
            self.db.execute("INSERT INTO folders(id,owner,name,normalized,thread_id) VALUES (?,?,?,?,?)",
                            (identity, owner, name, normalize(name), thread))
        return self.folder(owner, identity)

    def import_topic(self, owner, name, thread):
        existing = self.folder_for_thread(owner, thread)
        if existing:
            return existing
        name = clean_name(name)
        pending = self.one("SELECT * FROM folders WHERE owner=? AND normalized=? AND thread_id IS NULL",
                           (owner, normalize(name)))
        if pending:
            self.bind_thread(owner, pending["id"], thread)
            return self.folder(owner, pending["id"])
        try:
            return self.create_folder(owner, name, thread)
        except ValueError:
            return self.create_folder(owner, f"{name[:55]} ({thread})", thread)

    def check_names(self, owner, names, exclude=None):
        values = [normalize(n) for n in names]
        if len(set(values)) != len(values):
            raise ValueError("Названия и псевдонимы не должны повторяться")
        for folder in self.folders(owner):
            if folder["id"] != exclude:
                taken = [normalize(folder["name"]), *map(normalize, json.loads(folder["aliases"]))]
                if set(values) & set(taken):
                    raise ValueError("Такое название или псевдоним уже есть у другой папки")

    def rename_folder(self, owner, identity, name):
        folder = self.folder(owner, identity)
        if folder["is_inbox"]:
            raise ValueError("Папка «Входящие» имеет постоянное название")
        name = clean_name(name)
        self.check_names(owner, [name, *json.loads(folder["aliases"])], identity)
        with self.db:
            self.db.execute("UPDATE folders SET name=?,normalized=? WHERE id=? AND owner=?",
                            (name, normalize(name), identity, owner))

    def aliases(self, owner, identity, names):
        folder = self.folder(owner, identity)
        names = [clean_name(n) for n in names]
        if len(names) > 10:
            raise ValueError("Максимум 10 псевдонимов")
        self.check_names(owner, [folder["name"], *names], identity)
        with self.db:
            self.db.execute("UPDATE folders SET aliases=? WHERE id=? AND owner=?",
                            (json.dumps(names, ensure_ascii=False), identity, owner))

    def bind_thread(self, owner, identity, thread):
        self.folder(owner, identity)
        with self.db:
            self.db.execute("UPDATE folders SET thread_id=? WHERE id=? AND owner=?", (thread, identity, owner))

    def select(self, owner, identity):
        self.folder(owner, identity)
        with self.db:
            self.db.execute("UPDATE users SET active_folder=? WHERE id=?", (identity, owner))

    def set_action(self, owner, action):
        with self.db:
            self.db.execute("UPDATE users SET action=? WHERE id=?", (action, owner))

    def action(self, owner):
        return self.one("SELECT action FROM users WHERE id=?", (owner,))["action"]

    def add_note(self, owner, folder, message, kind, file_id=None, extension="txt", size=0,
                 duration=0, transcript=None):
        existing = self.one("SELECT * FROM notes WHERE owner=? AND message_id=?", (owner, message["message_id"]))
        if existing:
            return existing, False
        source_folder = self.folder(owner, folder)
        counters = self.one("SELECT count(*) AS n, coalesce(sum(max(audio_bytes,declared_bytes)),0) AS bytes, "
                            "coalesce(sum(state IN ('queued','processing')),0) AS pending "
                            "FROM notes WHERE owner=? AND deleted=0", (owner,))
        if counters["n"] >= self.limits.max_notes_per_user:
            raise ValueError("Достигнут лимит заметок; выгрузи архив и удали ненужное")
        if counters["pending"] >= self.limits.max_pending_per_user:
            raise ValueError("Очередь заполнена; дождись обработки предыдущих записей")
        if counters["bytes"] + size > self.limits.max_user_storage_bytes:
            raise ValueError("Достигнут лимит хранения аудио")
        identity = new_id()
        with self.db:
            self.db.execute("""INSERT INTO notes(id,owner,folder_id,source_folder,message_id,source_thread,
                kind,file_id,extension,declared_bytes,duration,created_at,transcript,input_mode,reply_message_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identity, owner, folder, folder, message["message_id"], message.get("message_thread_id"),
                 kind, file_id, extension, size, duration, time.time(), transcript, source_folder["mode"],
                 message.get("reply_to_message", {}).get("message_id")))
        return self.note(owner, identity), True

    def note(self, owner, identity):
        row = self.one("SELECT * FROM notes WHERE owner=? AND id=? AND deleted=0", (owner, identity))
        if not row:
            raise ValueError("Заметка не найдена")
        return row

    def notes(self, owner, folder=None, query=None, offset=0, limit=8):
        sql, args = "SELECT * FROM notes WHERE owner=? AND deleted=0 AND entry_type='note'", [owner]
        if folder:
            self.folder(owner, folder)
            sql += " AND folder_id=?"
            args.append(folder)
        if query:
            # Unicode search works for Russian as well; SQL LIKE is ASCII-case-insensitive only.
            words = normalize(query).split()[:8]
            rows = self.all(sql + " ORDER BY created_at DESC", args)
            matches = [r for r in rows if all(w in normalize(r["title"] + " " + (r["transcript"] or "")
                                                            + " " + (r["content"] or ""))
                                            for w in words)]
            return matches[offset:offset + limit]
        return self.all(sql + " ORDER BY created_at DESC LIMIT ? OFFSET ?", [*args, limit, offset])

    def export_notes(self, owner: int, folder=None):
        # Export all saved data even if a storage quota was lowered after the notes were created.
        sql, args = "SELECT * FROM notes WHERE owner=? AND deleted=0", [owner]
        if folder:
            self.folder(owner, folder)
            sql += " AND folder_id=?"
            args.append(folder)
        notes = self.all(sql + " ORDER BY created_at, id", args)
        selected = {n["id"]: n for n in notes}
        tables = {r["name"] for r in self.all("SELECT name FROM sqlite_master WHERE type='table'")}
        if "ai_requests" in tables:
            for row in self.all("SELECT note_id,response AS ai_response,state AS ai_state,conversation_id,"
                                "model AS ai_model FROM ai_requests WHERE owner=?", (owner,)):
                note_id = row.pop("note_id")
                if note_id in selected:
                    selected[note_id].update(row)
        if "note_revisions" in tables:
            rows = self.all("SELECT r.* FROM note_revisions r JOIN notes n ON n.id=r.note_id "
                            "WHERE n.owner=? AND n.deleted=0 ORDER BY r.revision", (owner,))
            for row in rows:
                note_id = row.pop("note_id")
                if note_id in selected:
                    selected[note_id].setdefault("revisions", []).append(row)
        return notes

    def export_preferences(self, owner: int):
        user = self.one("SELECT active_folder FROM users WHERE id=?", (owner,))
        result = {"text_format": self.export_format(owner), "active_folder": user["active_folder"] if user else None}
        if self.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_settings'"):
            ai = self.one("SELECT wake_enabled,names,model,notes_access FROM ai_settings WHERE owner=?", (owner,))
            if ai:
                ai["names"] = json.loads(ai["names"])
                result["ai"] = ai
        return result

    def update_note(self, owner, identity, **fields):
        allowed = {"state", "transcript", "title", "routing", "requested_folder", "error", "audio_bytes",
                   "sha256", "delivery", "result_message_id", "result_thread", "folder_id",
                   "status_message_id", "transcript_delivery", "transcript_message_id", "archive_dirty"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid note fields")
        before = self.note(owner, identity)
        if "folder_id" in fields:
            self.folder(owner, fields["folder_id"])
        assignments = ",".join(f"{key}=?" for key in fields)
        if before["state"] == "ready" and any(k in fields and fields[k] != before[k] for k in ("folder_id", "title")):
            assignments += ",revision=revision+1"
        with self.db:
            self.db.execute(f"UPDATE notes SET {assignments} WHERE id=? AND owner=? AND deleted=0",
                            [*fields.values(), identity, owner])

    def claim_note(self):
        row = self.one("SELECT * FROM notes WHERE state='queued' AND deleted=0 ORDER BY created_at LIMIT 1")
        if row:
            with self.db:
                self.db.execute("UPDATE notes SET state='processing',attempts=attempts+1 WHERE id=?", (row["id"],))
            return self.note(row["owner"], row["id"])

    def reserve_asr(self, owner, seconds):
        now = time.time()
        used = self.one("SELECT coalesce(sum(seconds),0) AS n FROM asr_usage WHERE owner=? AND at>?",
                        (owner, now - 86400))["n"]
        seconds = max(10, seconds)
        if used + seconds > self.limits.daily_asr_seconds:
            raise ValueError("daily_asr_limit")
        with self.db:
            self.db.execute("INSERT INTO asr_usage VALUES (?,?,?)", (owner, now, seconds))
            self.db.execute("DELETE FROM asr_usage WHERE at<?", (now - 86400,))

    def retry(self, owner, identity):
        note = self.note(owner, identity)
        if note["state"] not in {"asr_failed", "ready"}:
            raise ValueError("Эта запись уже обрабатывается")
        if note["state"] == "ready" and note["routing"] not in {"unavailable", "review"}:
            raise ValueError("Запись уже обработана; папку можно изменить вручную")
        if note["attempts"] >= 5:
            raise ValueError("Пять попыток уже использованы; аудио доступно для выгрузки")
        self.update_note(owner, identity, state="queued", error=None, delivery="pending")

    def delete(self, owner, identity):
        self.note(owner, identity)
        with self.db:
            self.db.execute("UPDATE notes SET deleted=1,transcript=NULL,title='',file_id=NULL,"
                            "content=NULL,requested_folder=NULL,error=NULL WHERE owner=? AND id=?", (owner, identity))
            self.db.execute("DELETE FROM note_revisions WHERE note_id=?", (identity,))
            self.db.execute("DELETE FROM ai_steps WHERE note_id=?", (identity,))
            self.db.execute("UPDATE ai_requests SET state='deleted',response=NULL,error=NULL WHERE note_id=?", (identity,))

    def ingest(self, updates, allowed):
        with self.db:
            for update in updates:
                message = update.get("message") or update.get("callback_query", {}).get("message", {})
                sender = update.get("callback_query", {}).get("from") or message.get("from", {})
                if sender.get("id") in allowed and message.get("chat", {}).get("type") == "private":
                    self.db.execute("INSERT OR IGNORE INTO updates(id,payload) VALUES (?,?)",
                                    (update["update_id"], json.dumps(update, ensure_ascii=False)))
                self.db.execute("INSERT INTO meta(key,value) VALUES ('offset',?) "
                                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (str(update["update_id"] + 1),))

    def offset(self):
        row = self.one("SELECT value FROM meta WHERE key='offset'")
        return int(row["value"]) if row else 0

    def pending_update(self):
        row = self.one("SELECT * FROM updates WHERE state='pending' ORDER BY id LIMIT 1")
        if row:
            with self.db:
                self.db.execute("UPDATE updates SET state='processing' WHERE id=?", (row["id"],))
            return json.loads(row["payload"])

    def finish_update(self, identity):
        # Drop payloads after handling; never keep a second copy of private messages in the queue.
        with self.db:
            self.db.execute("DELETE FROM updates WHERE id=?", (identity,))

    def fail_update(self, identity):
        # Preserve recoverable source data; replay requires inspection because a UI send may have succeeded.
        with self.db:
            self.db.execute("UPDATE updates SET state='failed' WHERE id=?", (identity,))


def safe_component(name: str) -> str:
    return re.sub(r"[^\w\-]", "_", name, flags=re.UNICODE).strip("._")[:60] or "project"
