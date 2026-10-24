from __future__ import annotations

import json
import logging
import time

from .providers import ProviderError

log = logging.getLogger(__name__)


def migrate_cleanup(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS telegram_messages (
            owner INTEGER NOT NULL, message_id INTEGER NOT NULL, thread_id INTEGER,
            PRIMARY KEY(owner, message_id)
        );
        CREATE TABLE IF NOT EXISTS deletion_jobs (
            owner INTEGER PRIMARY KEY, files_pending INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS deletion_targets (
            owner INTEGER NOT NULL, kind TEXT NOT NULL, identity INTEGER NOT NULL, thread_id INTEGER,
            PRIMARY KEY(owner, kind, identity)
        );
    """)


class CleanupStore:
    def __init__(self, store):
        self.store, self.db = store, store.db
        if "sent_at" not in {r[1] for r in self.db.execute("PRAGMA table_info(telegram_messages)")}:
            self.db.execute("ALTER TABLE telegram_messages ADD COLUMN sent_at REAL")

    def retire_telegram_cleanup(self):
        # Old Telegram-only retries must never touch new archive entries or notify users.
        with self.db:
            self.db.execute("DELETE FROM deletion_targets WHERE owner IN "
                            "(SELECT owner FROM deletion_jobs WHERE files_pending=0)")
            self.db.execute("DELETE FROM deletion_jobs WHERE files_pending=0")

    def remember(self, owner, message_id, thread=None, sent_at=None):
        # Only identifiers are needed; do not retain another copy of message contents.
        if type(owner) is not int or owner <= 0 or type(message_id) is not int or message_id <= 0:
            return
        with self.db:
            self.db.execute("INSERT INTO telegram_messages(owner,message_id,thread_id,sent_at) VALUES (?,?,?,?) ON CONFLICT(owner,message_id) "
                            "DO UPDATE SET thread_id=coalesce(excluded.thread_id,telegram_messages.thread_id)",
                            (owner, message_id, thread, time.time() if sent_at is None else sent_at))

    def job(self, owner):
        return self.store.one("SELECT * FROM deletion_jobs WHERE owner=?", (owner,))

    def targets(self, owner, kind):
        return self.store.all("SELECT * FROM deletion_targets WHERE owner=? AND kind=? ORDER BY identity",
                              (owner, kind))

    def begin(self, owner):
        messages = {r["message_id"]: r["thread_id"] for r in self.store.all(
            "SELECT * FROM telegram_messages WHERE owner=?", (owner,))}
        topics = {r["thread_id"] for r in self.store.all(
            "SELECT thread_id FROM folders WHERE owner=? UNION SELECT thread_id FROM implicit_topics WHERE owner=?",
            (owner, owner)) if r["thread_id"]}

        def remember(message_id, thread=None):
            if type(message_id) is int and message_id > 0:
                messages[message_id] = thread or messages.get(message_id)
            if thread:
                topics.add(thread)

        # Existing installations did not track every Telegram send. Recover all saved IDs,
        # including soft-deleted notes and the original locations of moved notes.
        for note in self.store.all("SELECT message_id,status_message_id,transcript_message_id,result_message_id,"
                                   "source_thread,result_thread FROM notes WHERE owner=?", (owner,)):
            for field, thread_field in (("message_id", "source_thread"), ("status_message_id", "source_thread"),
                                        ("transcript_message_id", "source_thread"), ("result_message_id", "result_thread")):
                remember(note[field], note[thread_field])
        for row in self.store.all("SELECT message_id FROM ai_links WHERE owner=?", (owner,)):
            remember(row["message_id"])

        tables = {r["name"] for r in self.store.all("SELECT name FROM sqlite_master WHERE type='table'")}
        if "note_messages" in tables:
            for row in self.store.all("SELECT message_id,thread_id FROM note_messages WHERE owner=?", (owner,)):
                remember(row["message_id"], row["thread_id"])

        updates = []
        for row in self.store.all("SELECT id,payload FROM updates"):
            update = json.loads(row["payload"])
            callback = update.get("callback_query", {})
            message = update.get("message") or callback.get("message", {})
            sender = callback.get("from") or message.get("from", {})
            if sender.get("id") == owner and message.get("chat", {}).get("id") == owner:
                updates.append((row["id"],))
                remember(message.get("message_id"), message.get("message_thread_id"))
        topics.update(thread for thread in messages.values() if thread)

        with self.db:
            # Intent, the Telegram snapshot, and database erasure commit together. A retry
            # only uses this snapshot and never erases notes created after the deletion.
            self.db.execute("INSERT INTO deletion_jobs(owner) VALUES (?) ON CONFLICT(owner) "
                            "DO UPDATE SET files_pending=1", (owner,))
            self.db.executemany("INSERT OR IGNORE INTO deletion_targets VALUES (?,'topic',?,NULL)",
                                [(owner, thread) for thread in topics])
            self.db.executemany("INSERT INTO deletion_targets VALUES (?,'message',?,?) "
                                "ON CONFLICT(owner,kind,identity) DO UPDATE SET "
                                "thread_id=coalesce(excluded.thread_id,deletion_targets.thread_id)",
                                [(owner, mid, thread) for mid, thread in messages.items()])
            self.db.execute("DELETE FROM note_revisions WHERE note_id IN (SELECT id FROM notes WHERE owner=?)", (owner,))
            self.db.execute("DELETE FROM ai_steps WHERE note_id IN (SELECT note_id FROM ai_requests WHERE owner=?)", (owner,))
            self.db.execute("DELETE FROM ai_progress WHERE note_id IN (SELECT note_id FROM ai_requests WHERE owner=?)", (owner,))
            for table in ("ai_message_jobs", "ai_note_jobs", "record_prompts", "record_panels", "note_moves", "note_messages", "folder_name_inputs",
                          "bottom_menus", "bottom_menu_labels", "folder_options"):
                if table in tables:
                    self.db.execute(f"DELETE FROM {table} WHERE owner=?", (owner,))
            for table in ("ai_requests", "ai_links", "ai_note_context", "ai_active", "ai_conversations", "notes", "folders", "implicit_topics",
                          "telegram_messages", "ai_settings", "user_export_settings", "user_message_settings", "asr_usage"):
                self.db.execute(f"DELETE FROM {table} WHERE owner=?", (owner,))
            if self.store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='folder_views'"):
                self.db.execute("DELETE FROM folder_views WHERE owner=?", (owner,))
            self.db.execute("UPDATE users SET active_folder=NULL,action=NULL WHERE id=?", (owner,))
            self.db.executemany("DELETE FROM updates WHERE id=?", updates)

    def files_done(self, owner):
        with self.db:
            self.db.execute("UPDATE deletion_jobs SET files_pending=0 WHERE owner=?", (owner,))

    def done(self, owner, kind, identities):
        with self.db:
            self.db.executemany("DELETE FROM deletion_targets WHERE owner=? AND kind=? AND identity=?",
                                [(owner, kind, identity) for identity in identities])
            if kind == "topic":
                self.db.executemany("DELETE FROM deletion_targets WHERE owner=? AND kind='message' AND thread_id=?",
                                    [(owner, identity) for identity in identities])

    def finish(self, owner):
        with self.db:
            self.db.execute("DELETE FROM deletion_targets WHERE owner=?", (owner,))
            self.db.execute("DELETE FROM deletion_jobs WHERE owner=? AND files_pending=0", (owner,))


class CleanupMixin:
    def init_cleanup(self):
        self.cleanup = CleanupStore(self.store)
        self.cleanup.retire_telegram_cleanup()
        self.telegram.on_message = self.cleanup.remember
        self.delete_confirmations = {}
        self.deleting_owners = set()

    async def delete_all_menu(self, owner, thread=None, edit_id=None):
        from .bot import button, keyboard
        from .store import new_id
        self.store.set_action(owner, None)
        token = new_id()
        self.delete_confirmations[owner] = (token, time.monotonic() + 600)
        count = self.store.one("SELECT count(*) AS n FROM notes WHERE owner=? AND deleted=0", (owner,))["n"]
        text = ("Удалить всё?\n\n"
                f"Папок: {len(self.store.folders(owner))}. Записей и сообщений ИИ: {count}.\n"
                "Будут удалены все папки, тексты, аудио, фото, версии заметок и история ИИ, "
                "настройки и данные использования. В Telegram удалим доступные боту темы и сообщения.\n\n"
                "Это нельзя отменить. Сообщения вне тем старше 48 часов и старые сообщения, "
                "которые бот не сохранил, могут остаться в Telegram. Скачанные архивы останутся у тебя.")
        await self.show_bottom(owner, text, thread, keyboard(
            [button("🗑 Да, удалить всё", f"confirmdeleteall:{token}")]),
            screen="deleteall", back="settings:all", keep=True, edit_id=edit_id)

    def deletion_busy(self, owner):
        notes = self.store.all("SELECT id,state,delivery,transcript_delivery FROM notes WHERE owner=? AND deleted=0",
                               (owner,))
        return (self.export_locks[owner].locked()
                or self.ai_note_actions.locks[owner].locked()
                or bool(getattr(self, "web", None) and owner in self.web.uploading)
                or any(n["state"] == "processing" or n["delivery"] == "sending"
                       or n["transcript_delivery"] == "sending"
                       or self.note_locks[n["id"]].locked() or self.ai_delivery_locks[n["id"]].locked()
                       for n in notes)
                or bool(self.store.one("SELECT 1 FROM ai_requests WHERE owner=? "
                                       "AND (state='running' OR delivery='sending')", (owner,))))

    async def cleanup_callback(self, owner, thread, message_id, data):
        from .bot import HOME
        action, _, token = data.partition(":")
        if action == "deleteall":
            await self.delete_all_menu(owner, thread, message_id)
        elif action in {"confirmdeleteall", "canceldeleteall"}:
            confirmation = self.delete_confirmations.get(owner)
            if not confirmation or confirmation[0] != token or time.monotonic() >= confirmation[1]:
                raise ValueError("Подтверждение устарело. Снова выбери «Удалить всё».")
            if action == "canceldeleteall":
                self.delete_confirmations.pop(owner, None)
                await self.show(owner, "Удаление отменено.", thread, HOME, message_id)
            else:
                if self.deletion_busy(owner):
                    raise ValueError("Дождись завершения обработки сообщений и экспорта, затем подтверди удаление ещё раз.")
                # No await between the busy check and erasure: workers cannot claim old data.
                self.cleanup.begin(owner)
                self.delete_confirmations.pop(owner, None)
                await self.resume_delete_all(owner)
        elif action == "retrydeleteall":
            await self.resume_delete_all(owner)
        else:
            return False
        return True

    async def delete_telegram_snapshot(self, owner):
        for target in self.cleanup.targets(owner, "topic"):
            identity = target["identity"]
            try:
                await self.telegram.call("deleteForumTopic", chat_id=owner, message_thread_id=identity)
            except ProviderError as exc:
                if getattr(exc, "reason", None) not in {"thread_not_found", "thread_invalid", "topic_deleted"}:
                    log.warning("topic cleanup failed owner=%s error=%s", owner, exc)
                    if str(exc) != "telegram_400":
                        return
                    continue
            self.cleanup.done(owner, "topic", [identity])

        messages = [t["identity"] for t in self.cleanup.targets(owner, "message")]
        for offset in range(0, len(messages), 100):
            batch = messages[offset:offset + 100]
            try:
                await self.telegram.call("deleteMessages", chat_id=owner, message_ids=batch)
            except ProviderError as exc:
                if str(exc) != "telegram_400":
                    return
                # One old or protected message must not prevent deletion of the others.
                for identity in batch:
                    try:
                        await self.telegram.call("deleteMessage", chat_id=owner, message_id=identity)
                    except ProviderError as single:
                        if getattr(single, "reason", None) != "delete_target_missing":
                            if str(single) != "telegram_400":
                                return
                            continue
                    self.cleanup.done(owner, "message", [identity])
            else:
                self.cleanup.done(owner, "message", batch)

    async def resume_delete_all(self, owner):
        from .bot import HOME, button, keyboard
        if owner in self.deleting_owners:
            return
        job = self.cleanup.job(owner)
        if not job:
            return await self.show(owner, "Очистка уже завершена.", keys=HOME)
        self.deleting_owners.add(owner)
        try:
            if job["files_pending"]:
                try:
                    self.archive.delete_all(owner)
                except OSError:
                    log.warning("archive cleanup failed owner=%s", owner)
                    return await self.show(owner, "Записи удалены из базы, но очистка файлов ещё не завершена. "
                                           "Повтори удаление перед отправкой новых заметок.",
                                           keys=keyboard([button("Повторить очистку", "retrydeleteall")]))
                self.cleanup.files_done(owner)
            await self.delete_telegram_snapshot(owner)
            topics = len(self.cleanup.targets(owner, "topic"))
            messages = len(self.cleanup.targets(owner, "message"))
            # Telegram is best effort, once per confirmed deletion. Persist completion
            # before sending the report so a lost response cannot create restart spam.
            self.cleanup.finish(owner)
            text = "Удалено: все папки, записи, аудио, фото, файлы архива, история ИИ и настройки."
            if topics or messages:
                text += "\nЧасть истории Telegram удалить не удалось. Автоматических повторов не будет."
            else:
                text += "\nДоступные боту темы и сообщения Telegram удалены."
            text += "\nОставшуюся историю можно убрать через «Очистить историю» в Telegram."
            await self.show(owner, text, keys=HOME)
        finally:
            self.deleting_owners.discard(owner)

    async def resume_deletions(self):
        for job in self.store.all("SELECT owner FROM deletion_jobs"):
            if job["owner"] in self.config.allowed_user_ids:
                try:
                    await self.resume_delete_all(job["owner"])
                except ProviderError:
                    log.warning("cleanup result delivery failed owner=%s", job["owner"])
