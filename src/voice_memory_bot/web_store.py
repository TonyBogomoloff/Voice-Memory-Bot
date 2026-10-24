"""Additional metadata for browser uploads in the existing private archive."""

import time


def migrate_web(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(notes)")}
    with db:
        for name, declaration in {
            "origin": "TEXT NOT NULL DEFAULT 'telegram'",
            "original_name": "TEXT",
            "mime_type": "TEXT",
            "upload_key": "TEXT",
            "ai_context": "TEXT",
            "ai_generated_from": "TEXT",
            "source_json": "TEXT",
        }.items():
            if name not in columns:
                db.execute(f"ALTER TABLE notes ADD COLUMN {name} {declaration}")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS web_upload_key ON notes(owner,upload_key) "
                   "WHERE upload_key IS NOT NULL")


def create_text(store, owner, folder, text, key, context=None, generated_from=None):
    """Called inside the caller's transaction; acceptance and idempotency commit together."""
    if not isinstance(text, str) or not text.strip() or len(text) > 40000:
        raise ValueError("Текст должен содержать от 1 до 40000 символов")
    if context:
        if not isinstance(context, str) or len(context) != 16 or not context.isalnum():
            raise ValueError("Выбери исходную запись заново")
        store.note(owner, context)
    old = store.one("SELECT * FROM notes WHERE owner=? AND upload_key=?", (owner, key))
    if old:
        if old["deleted"]:
            raise ValueError("Эта запись уже удалена")
        return old, False
    note, created = store.add_note(owner, folder, {"message_id": -(int(key[:15], 16) + 1)},
                                   "text", transcript=text, commit=False)
    if not created:
        raise ValueError("Повтори сохранение с новым идентификатором")
    store.db.execute("UPDATE notes SET state='ready',origin='web',upload_key=?,title=?,ai_context=?,"
                     "ai_generated_from=?,routing='manual',delivery='sent',transcript_delivery='sent',"
                     "archive_dirty=1 WHERE id=? AND owner=?",
                     (key, "Ответ ИИ" if generated_from else "Текстовая заметка", context, generated_from,
                      note["id"], owner))
    return store.note(owner, note["id"]), True


def result_text(store, source, text, *, created_at=None):
    """Archive an accepted AI result inside the caller's transaction.

    Intake quotas must not discard a reply after its request has been accepted.
    This also preserves complete historical replies without the web-input length limit.
    """
    from .store import new_id
    store.note(source["owner"], source["id"])
    identity = new_id()
    message = store.one("SELECT min(0,coalesce(min(message_id),0))-1 AS id FROM notes WHERE owner=?",
                        (source["owner"],))["id"]
    store.db.execute("""INSERT INTO notes(id,owner,folder_id,source_folder,message_id,source_thread,
        kind,extension,created_at,state,transcript,title,origin,ai_generated_from,routing,
        delivery,transcript_delivery,archive_dirty)
        VALUES (?,?,?,?,?,?,'text','txt',?,'ready',?,'Ответ ИИ',?,?,'manual','sent','sent',1)""",
        (identity, source["owner"], source["folder_id"], source["folder_id"], message, source["source_thread"],
         time.time() if created_at is None else created_at, text,
         "web" if source["origin"] == "web" else "ai", source["id"]))
    return store.note(source["owner"], identity)
