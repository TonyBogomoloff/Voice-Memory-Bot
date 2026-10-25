from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
import uuid
from contextlib import nullcontext
from pathlib import Path

from .ai_store import migrate_ai
from .cleanup import migrate_cleanup
from .config import Limits

MAX_PINNED_FOLDERS = 4


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
            CREATE TABLE IF NOT EXISTS user_message_settings (
                owner INTEGER PRIMARY KEY REFERENCES users(id),
                buttons_enabled INTEGER NOT NULL DEFAULT 0 CHECK(buttons_enabled IN (0, 1))
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
        with self.db:
            for name, declaration in {
                "origin": "TEXT NOT NULL DEFAULT 'telegram'",
                "original_name": "TEXT",
                "mime_type": "TEXT",
                "ai_generated_from": "TEXT",
                "source_json": "TEXT",
            }.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE notes ADD COLUMN {name} {declaration}")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if "pin_slot" not in {row["name"] for row in self.all("PRAGMA table_info(folders)")}:
                self.db.execute("ALTER TABLE folders ADD COLUMN pin_slot INTEGER "
                                "CHECK(pin_slot BETWEEN 1 AND 4)")
                for user in self.all("SELECT id FROM users"):
                    folders = self.all("SELECT id FROM folders WHERE owner=? "
                                       "ORDER BY is_inbox DESC, normalized LIMIT ?",
                                       (user["id"], MAX_PINNED_FOLDERS))
                    for slot, folder in enumerate(folders, 1):
                        self.db.execute("UPDATE folders SET pin_slot=? WHERE id=?", (slot, folder["id"]))
            if "removed" not in {row["name"] for row in self.all("PRAGMA table_info(folders)")}:
                self.db.execute("ALTER TABLE folders ADD COLUMN removed INTEGER NOT NULL DEFAULT 0")
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS folder_pin_slot "
                            "ON folders(owner,pin_slot) WHERE pin_slot IS NOT NULL")

    def close(self):
        self.db.close()

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]

    def register_user(self, owner: int):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (owner,))

    def ensure_user(self, owner: int) -> str:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (owner,))
            folder = self.one("SELECT id FROM folders WHERE owner=? AND is_inbox=1 AND removed=0", (owner,))
            if folder:
                return folder["id"]
            identity = new_id()
            self.db.execute("INSERT INTO folders(id,owner,name,normalized,is_inbox,pin_slot) VALUES (?,?,?,?,1,?)",
                            (identity, owner, "Входящие", normalize("Входящие"), self.free_pin_slot(owner)))
            return identity

    def folders(self, owner: int, include_removed=False):
        return self.all("SELECT f.*, (SELECT count(*) FROM notes n WHERE n.folder_id=f.id "
                        "AND n.deleted=0 AND n.entry_type='note') AS count FROM folders f WHERE owner=? AND (removed=0 OR ?) "
                        "ORDER BY is_inbox DESC, normalized", (owner, int(include_removed)))

    def pinned_folders(self, owner):
        return self.all("SELECT * FROM folders WHERE owner=? AND removed=0 AND pin_slot IS NOT NULL ORDER BY pin_slot",
                        (owner,))

    def free_pin_slot(self, owner):
        used = {folder["pin_slot"] for folder in self.pinned_folders(owner)}
        return next((slot for slot in range(1, MAX_PINNED_FOLDERS + 1) if slot not in used), None)

    def set_folder_pin(self, owner, identity, pinned):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            folder = self.folder(owner, identity)
            if folder['removed']:
                raise ValueError('Папка удалена')
            slot = folder["pin_slot"] if pinned else None
            if pinned and slot is None:
                slot = self.free_pin_slot(owner)
                if slot is None:
                    raise ValueError("Внизу уже 4 папки. Открой одну из них и нажми «Убрать снизу», "
                                     "затем закрепи нужную.")
            self.db.execute("UPDATE folders SET pin_slot=? WHERE owner=? AND id=?", (slot, owner, identity))

    def message_buttons_enabled(self, owner: int) -> bool:
        row = self.one("SELECT buttons_enabled FROM user_message_settings WHERE owner=?", (owner,))
        return bool(row and row["buttons_enabled"])

    def set_message_buttons(self, owner: int, enabled: bool):
        if type(enabled) is not bool:
            raise ValueError("Выбери, показывать ли кнопки у сообщений")
        self.register_user(owner)
        with self.db:
            self.db.execute("INSERT INTO user_message_settings(owner,buttons_enabled) VALUES (?,?) "
                            "ON CONFLICT(owner) DO UPDATE SET buttons_enabled=excluded.buttons_enabled",
                            (owner, int(enabled)))

    def export_format(self, owner: int) -> str:
        row = self.one("SELECT text_format FROM user_export_settings WHERE owner=?", (owner,))
        return row["text_format"] if row else "pdf"

    def set_export_format(self, owner: int, text_format: str):
        if text_format not in {"pdf", "markdown"}:
            raise ValueError("Выбери PDF или Markdown")
        self.register_user(owner)
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

    def current_folder(self, owner: int, thread: int | None = None):
        self.register_user(owner)
        selected = self.one("SELECT f.id FROM users u JOIN folders f "
                            "ON f.id=u.active_folder AND f.owner=u.id AND f.removed=0 WHERE u.id=?", (owner,))
        folders = self.folders(owner)
        return selected["id"] if selected else folders[0]["id"] if folders else None

    def delete_folder(self, owner, identity):
        """Hide any folder, preserving the archive under its stable historical ID."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            folder = self.folder(owner, identity)
            if folder["removed"]:
                raise ValueError("Папка уже удалена")
            if self.one("SELECT 1 FROM notes WHERE owner=? AND deleted=0 AND "
                        "(folder_id=? OR source_folder=?) AND (state IN ('queued','processing') "
                        "OR delivery='sending' OR transcript_delivery='sending')", (owner, identity, identity)) or self.one(
                            "SELECT 1 FROM ai_requests WHERE owner=? AND "
                            "(state IN ('queued','running') OR delivery='sending')", (owner,)):
                raise ValueError("Дождись завершения обработки записей и ИИ, затем удали папку")
            self.db.execute("UPDATE folders SET removed=1,pin_slot=NULL,normalized=? WHERE owner=? AND id=?",
                            ("removed:" + identity, owner, identity))
            remaining = self.folders(owner)
            active = self.one('SELECT active_folder FROM users WHERE id=?', (owner,))['active_folder']
            selected = active if any(f['id'] == active for f in remaining) else remaining[0]['id'] if remaining else None
            self.db.execute("UPDATE users SET active_folder=?,action=NULL WHERE id=? AND active_folder=?",
                            (selected, owner, identity))
            self.db.execute("DELETE FROM ai_active WHERE owner=? AND folder_id=?", (owner, identity))
            if folder["thread_id"]:
                self.db.execute("INSERT OR IGNORE INTO implicit_topics VALUES (?,?)", (owner, folder["thread_id"]))
        return selected

    def create_folder(self, owner: int, name: str, thread: int | None = None, *, commit=True):
        if commit:
            self.register_user(owner)
        name = clean_name(name)
        self.check_names(owner, [name])
        if len(self.folders(owner)) >= self.limits.max_folders_per_user:
            raise ValueError("Достигнут лимит папок")
        identity = new_id()
        with self.db if commit else nullcontext():
            if commit:
                self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT INTO folders(id,owner,name,normalized,thread_id,pin_slot) VALUES (?,?,?,?,?,?)",
                            (identity, owner, name, normalize(name), thread, self.free_pin_slot(owner)))
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
        return self.create_folder(owner, name, thread)

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
        if folder["removed"]:
            raise ValueError("Папка удалена")
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
        if self.folder(owner, identity)["removed"]:
            raise ValueError("Папка удалена. Выбери другую")
        with self.db:
            self.db.execute("UPDATE users SET active_folder=? WHERE id=?", (identity, owner))

    def set_action(self, owner, action):
        with self.db:
            self.db.execute("UPDATE users SET action=? WHERE id=?", (action, owner))

    def action(self, owner):
        return self.one("SELECT action FROM users WHERE id=?", (owner,))["action"]

    def add_note(self, owner, folder, message, kind, file_id=None, extension="txt", size=0,
                 duration=0, transcript=None, *, commit=True):
        existing = self.one("SELECT * FROM notes WHERE owner=? AND message_id=?", (owner, message["message_id"]))
        if existing:
            return existing, False
        source_folder = self.folder(owner, folder)
        if source_folder['removed']:
            raise ValueError('Выбери существующую папку')
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
        media = message.get("document" if kind == "file" else kind)
        media = media if isinstance(media, dict) else {}
        with self.db if commit else nullcontext():
            self.db.execute("""INSERT INTO notes(id,owner,folder_id,source_folder,message_id,source_thread,
                kind,file_id,extension,declared_bytes,duration,created_at,transcript,input_mode,reply_message_id,
                source_json,original_name,mime_type)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identity, owner, folder, folder, message["message_id"], message.get("message_thread_id"),
                 kind, file_id, extension, size, duration, time.time(), transcript, source_folder["mode"],
                 message.get("reply_to_message", {}).get("message_id"),
                 json.dumps({key: message[key] for key in (
                     "message_id", "date", "text", "entities", "caption", "caption_entities",
                     "voice", "video_note", "video", "audio", "document", "photo", "reply_to_message") if key in message},
                     ensure_ascii=False),
                 media.get("file_name"), media.get("mime_type")))
        return self.note(owner, identity), True

    def note(self, owner, identity):
        row = self.one("SELECT * FROM notes WHERE owner=? AND id=? AND deleted=0", (owner, identity))
        if not row:
            raise ValueError("Заметка не найдена")
        return row

    def notes(self, owner, folder=None, query=None, offset=0, limit=8):
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if limit is not None and (type(limit) is not int or limit < -1):
            raise ValueError("limit must be None or a non-negative integer")
        # Keep the old SQL LIMIT -1 contract for callers saved before this change.
        if limit == -1:
            limit = None
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
            return matches[offset:] if limit is None else matches[offset:offset + limit]
        return self.all(sql + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                        [*args, -1 if limit is None else limit, offset])

    def export_notes(self, owner: int, folder=None):
        # Export all saved data even if a storage quota was lowered after the notes were created.
        sql, args = "SELECT * FROM notes WHERE owner=? AND deleted=0 AND entry_type!='ai_history'", [owner]
        if folder:
            self.folder(owner, folder)
            sql += " AND folder_id=?"
            args.append(folder)
        notes = self.all(sql + " ORDER BY created_at, id", args)
        selected = {n["id"]: n for n in notes}
        history_only = {n['id'] for n in self.all("SELECT id FROM notes WHERE owner=? AND entry_type='ai_history'", (owner,))}
        tables = {r["name"] for r in self.all("SELECT name FROM sqlite_master WHERE type='table'")}
        if "ai_requests" in tables:
            for row in self.all("SELECT note_id,response AS ai_response,state AS ai_state,conversation_id,"
                                "model AS ai_model,result_note_id FROM ai_requests WHERE owner=?", (owner,)):
                note_id = row.pop("note_id")
                result_note_id = row.pop("result_note_id")
                if note_id in selected:
                    # A standalone answer already includes the complete original text.
                    if result_note_id in selected or result_note_id in history_only:
                        row.pop("ai_response")
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
                   "status_message_id", "transcript_delivery", "transcript_message_id", "archive_dirty",
                   "origin", "original_name", "mime_type", "duration"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid note fields")
        before = self.note(owner, identity)
        if "folder_id" in fields:
            if self.folder(owner, fields["folder_id"])["removed"]:
                raise ValueError("Папка удалена. Выбери другую")
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

    def delete(self, owner, identity, *, commit=True):
        self.note(owner, identity)
        with self.db if commit else nullcontext():
            self.db.execute("UPDATE notes SET deleted=1,transcript=NULL,title='',file_id=NULL,"
                            "content=NULL,source_json=NULL,original_name=NULL,mime_type=NULL,"
                            "requested_folder=NULL,error=NULL WHERE owner=? AND id=?", (owner, identity))
            self.db.execute("DELETE FROM note_revisions WHERE note_id=?", (identity,))
            self.db.execute("UPDATE ai_note_context SET content='',title='Удалённая запись' WHERE note_id=?",
                            (identity,))
            self.db.execute("DELETE FROM ai_steps WHERE note_id=?", (identity,))
            self.db.execute("UPDATE ai_requests SET state='deleted',response=NULL,input_text=NULL,error=NULL WHERE note_id=?", (identity,))

    def edit_source(self, owner, message):
        """Apply source text/caption edits without replaying commands or AI requests."""
        note = self.one("SELECT * FROM notes WHERE owner=? AND message_id=? AND deleted=0",
                        (owner, message.get("message_id")))
        if not note:
            return
        source = json.loads(note["source_json"] or "{}")
        if message.get("edit_date", 0) < source.get("edit_date", 0):
            return
        updated = dict(source)
        for key in ("text", "entities", "caption", "caption_entities"):
            updated.pop(key, None)
            if key in message:
                updated[key] = message[key]
        updated["edit_date"] = message.get("edit_date", 0)
        if updated == source:
            return
        # Audio transcripts remain ASR output; their captions live in source_json.
        field = "text" if note["kind"] == "text" else "caption"
        changed = source.get(field, "") != updated.get(field, "")
        replace_text = changed and note["kind"] in {"text", "photo", "file", "video"}
        with self.db:
            self.db.execute("UPDATE notes SET source_json=?,archive_dirty=1,revision=revision+?,"
                            "transcript=CASE WHEN ? THEN ? ELSE transcript END,"
                            "content=CASE WHEN ? THEN NULL ELSE content END "
                            "WHERE owner=? AND id=? AND deleted=0",
                            (json.dumps(updated, ensure_ascii=False), int(changed), replace_text,
                             updated.get(field, ""), replace_text, owner, note["id"]))

    def ingest(self, updates, allowed):
        with self.db:
            for update in updates:
                message = update.get("message") or update.get("edited_message") or update.get("callback_query", {}).get("message", {})
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
