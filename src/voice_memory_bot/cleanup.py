"""Telegram receipt tracking and guards for record-scoped cleanup."""
import time


class CleanupStore:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def remember(self, owner, message_id, thread=None, sent_at=None):
        # Only identifiers are needed; do not retain another copy of message contents.
        if type(owner) is not int or owner <= 0 or type(message_id) is not int or message_id <= 0:
            return
        with self.store.transaction():
            self.db.execute("INSERT INTO telegram_messages(owner,message_id,thread_id,sent_at) VALUES (?,?,?,?) ON CONFLICT(owner,message_id) "
                            "DO UPDATE SET thread_id=coalesce(excluded.thread_id,telegram_messages.thread_id)",
                            (owner, message_id, thread, time.time() if sent_at is None else sent_at))


class CleanupMixin:
    def init_cleanup(self):
        self.cleanup = CleanupStore(self.store)
        self.telegram.on_message = self.cleanup.remember

    def deletion_busy(self, owner):
        notes = self.store.all("SELECT id,state,delivery,transcript_delivery FROM notes WHERE owner=? AND deleted=0",
                               (owner,))
        return (self.export_locks[owner].locked()
                or self.ai_note_actions.locks[owner].locked()
                or any(n["state"] == "processing" or n["delivery"] == "sending"
                       or n["transcript_delivery"] == "sending"
                       or self.note_locks[n["id"]].locked() or self.ai_delivery_locks[n["id"]].locked()
                       for n in notes)
                or bool(self.store.one("SELECT 1 FROM ai_requests WHERE owner=? "
                                       "AND (state='running' OR delivery='sending')", (owner,))))
