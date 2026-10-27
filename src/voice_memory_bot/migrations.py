"""Versioned, additive schema changes and explicit runtime recovery.

Version 1 adopts every pre-versioned release without dropping legacy upload columns.
Future changes must append a migration instead of changing a completed version.
"""
import sqlite3

from .database import transaction


def execute_script(db, script):
    """Unlike Connection.executescript, preserve the caller's transaction."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        db.execute(statement)


def _migrate_ai(db):
    """Additive migration; raw transcripts and existing note IDs remain unchanged."""
    with transaction(db):
        for table, fields in {
            "folders": {"mode": "TEXT NOT NULL DEFAULT 'notes'"},
            "notes": {"entry_type": "TEXT NOT NULL DEFAULT 'note'", "content": "TEXT",
                      "reply_message_id": "INTEGER", "input_mode": "TEXT NOT NULL DEFAULT 'notes'",
                      "revision": "INTEGER NOT NULL DEFAULT 0", "archive_dirty": "INTEGER NOT NULL DEFAULT 0"},
        }.items():
            columns = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            for name, definition in fields.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    execute_script(db, """
        CREATE TABLE IF NOT EXISTS ai_settings (
            owner INTEGER PRIMARY KEY REFERENCES users(id), wake_enabled INTEGER NOT NULL DEFAULT 0,
            names TEXT NOT NULL DEFAULT '["Луна", "Астра", "GPT", "ChatGPT", "джипити"]',
            model TEXT, notes_access TEXT NOT NULL DEFAULT 'write'
        );
        CREATE TABLE IF NOT EXISTS ai_conversations (
            id TEXT PRIMARY KEY, owner INTEGER NOT NULL REFERENCES users(id),
            folder_id TEXT NOT NULL REFERENCES folders(id), created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_note_context (
            conversation_id TEXT PRIMARY KEY REFERENCES ai_conversations(id),
            owner INTEGER NOT NULL, note_id TEXT NOT NULL REFERENCES notes(id),
            title TEXT NOT NULL, content TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_active (
            owner INTEGER NOT NULL, folder_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
            PRIMARY KEY(owner, folder_id)
        );
        CREATE TABLE IF NOT EXISTS ai_requests (
            note_id TEXT PRIMARY KEY REFERENCES notes(id), owner INTEGER NOT NULL,
            conversation_id TEXT NOT NULL REFERENCES ai_conversations(id),
            trigger TEXT NOT NULL, thread_id INTEGER, reply_to INTEGER,
            state TEXT NOT NULL DEFAULT 'queued', model TEXT, response TEXT, error TEXT,
            delivery TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_links (
            owner INTEGER NOT NULL, message_id INTEGER NOT NULL, conversation_id TEXT NOT NULL,
            PRIMARY KEY(owner, message_id)
        );
        CREATE TABLE IF NOT EXISTS ai_progress (
            note_id TEXT PRIMARY KEY REFERENCES ai_requests(note_id), message_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_steps (
            note_id TEXT NOT NULL REFERENCES ai_requests(note_id), step INTEGER NOT NULL,
            command TEXT NOT NULL, result TEXT, PRIMARY KEY(note_id, step)
        );
        CREATE TABLE IF NOT EXISTS note_revisions (
            note_id TEXT NOT NULL REFERENCES notes(id), revision INTEGER NOT NULL,
            title TEXT NOT NULL, content TEXT, folder_id TEXT NOT NULL,
            request_id TEXT, created_at REAL NOT NULL, PRIMARY KEY(note_id, revision)
        );
        CREATE INDEX IF NOT EXISTS ai_request_queue ON ai_requests(state, created_at);
        CREATE TABLE IF NOT EXISTS ai_note_jobs (
            request_id TEXT NOT NULL REFERENCES ai_requests(note_id), step INTEGER NOT NULL,
            owner INTEGER NOT NULL, note_id TEXT NOT NULL REFERENCES notes(id), action TEXT NOT NULL,
            state TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(request_id, step)
        );
        CREATE TABLE IF NOT EXISTS ai_message_jobs (
            request_id TEXT NOT NULL REFERENCES ai_requests(note_id), step INTEGER NOT NULL,
            owner INTEGER NOT NULL, note_id TEXT NOT NULL REFERENCES notes(id), action TEXT NOT NULL,
            state TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(request_id, step, note_id)
        );
        INSERT OR IGNORE INTO ai_message_jobs SELECT * FROM ai_note_jobs;
    """)
    with transaction(db):
        columns = {r[1] for r in db.execute("PRAGMA table_info(ai_requests)")}
        for name in ("input_text", "result_note_id", "reply_context"):
            if name not in columns:
                db.execute(f"ALTER TABLE ai_requests ADD COLUMN {name} TEXT")


def _migrate_cleanup(db):
    execute_script(db, """
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


def baseline(store):
    execute_script(store.db, """
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
    with store.transaction():
        columns = {row["name"] for row in store.all("PRAGMA table_info(notes)")}
        if "status_message_id" not in columns:
            store.db.execute("ALTER TABLE notes ADD COLUMN status_message_id INTEGER")
        if "transcript_delivery" not in columns:
            store.db.execute("ALTER TABLE notes ADD COLUMN transcript_delivery TEXT NOT NULL DEFAULT 'pending'")
            store.db.execute("UPDATE notes SET transcript_delivery=delivery "
                            "WHERE delivery!='pending' AND transcript IS NOT NULL")
        if "transcript_message_id" not in columns:
            store.db.execute("ALTER TABLE notes ADD COLUMN transcript_message_id INTEGER")
    _migrate_ai(store.db)
    _migrate_cleanup(store.db)
    with store.transaction():
        for name, declaration in {
            "origin": "TEXT NOT NULL DEFAULT 'telegram'",
            "original_name": "TEXT",
            "mime_type": "TEXT",
            "ai_generated_from": "TEXT",
            "source_json": "TEXT",
        }.items():
            if name not in columns:
                store.db.execute(f"ALTER TABLE notes ADD COLUMN {name} {declaration}")
    with store.transaction():
        if "pin_slot" not in {row["name"] for row in store.all("PRAGMA table_info(folders)")}:
            store.db.execute("ALTER TABLE folders ADD COLUMN pin_slot INTEGER "
                            "CHECK(pin_slot BETWEEN 1 AND 4)")
            for user in store.all("SELECT id FROM users"):
                folders = store.all("SELECT id FROM folders WHERE owner=? "
                                   "ORDER BY is_inbox DESC, normalized LIMIT ?",
                                   (user["id"], 4))
                for slot, folder in enumerate(folders, 1):
                    store.db.execute("UPDATE folders SET pin_slot=? WHERE id=?", (slot, folder["id"]))
        if "removed" not in {row["name"] for row in store.all("PRAGMA table_info(folders)")}:
            store.db.execute("ALTER TABLE folders ADD COLUMN removed INTEGER NOT NULL DEFAULT 0")
        store.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS folder_pin_slot "
                        "ON folders(owner,pin_slot) WHERE pin_slot IS NOT NULL")

    execute_script(store.db, """
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
    with store.transaction():
        # Old versions kept only these exact IDs. Never infer adjacent message IDs.
        for field, thread, kind in (("message_id", "source_thread", "source"),
                                    ("transcript_message_id", "source_thread", "transcript"),
                                    ("result_message_id", "result_thread", "result")):
            store.db.execute(f"""INSERT OR IGNORE INTO note_messages(owner,message_id,note_id,thread_id,kind)
                SELECT owner,{field},id,{thread},? FROM notes WHERE {field}>0 AND deleted=0""", (kind,))

    if "sent_at" not in {r[1] for r in store.db.execute("PRAGMA table_info(telegram_messages)")}:
        store.db.execute("ALTER TABLE telegram_messages ADD COLUMN sent_at REAL")

    if "view_blocked" not in {r[1] for r in store.db.execute("PRAGMA table_info(telegram_messages)")}:
        store.db.execute("ALTER TABLE telegram_messages ADD COLUMN view_blocked INTEGER NOT NULL DEFAULT 0")
    execute_script(store.db, """
        CREATE TABLE IF NOT EXISTS folder_views (
            owner INTEGER PRIMARY KEY, folder_id TEXT NOT NULL, thread_id INTEGER,
            page INTEGER NOT NULL DEFAULT 0, dirty INTEGER NOT NULL DEFAULT 1
        );
    """)
    if "screen" not in {r[1] for r in store.db.execute("PRAGMA table_info(folder_views)")}:
        store.db.execute("ALTER TABLE folder_views ADD COLUMN screen TEXT")
    if "command_message_id" not in {r[1] for r in store.db.execute("PRAGMA table_info(folder_views)")}:
        store.db.execute("ALTER TABLE folder_views ADD COLUMN command_message_id INTEGER")

    execute_script(store.db, """
        CREATE TABLE IF NOT EXISTS bottom_menus (
            owner INTEGER PRIMARY KEY, state TEXT NOT NULL, message_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS bottom_menu_labels (
            owner INTEGER NOT NULL, label TEXT NOT NULL, PRIMARY KEY(owner,label)
        );
    """)

    with store.transaction():
        store.db.execute('CREATE TABLE IF NOT EXISTS emoji_keyboard_labels ('
                         'owner INTEGER NOT NULL, label TEXT NOT NULL, original TEXT NOT NULL, '
                         'active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(owner,label))')

    store.db.execute("""CREATE TABLE IF NOT EXISTS reply_moves (
        token TEXT PRIMARY KEY, owner INTEGER NOT NULL, note_id TEXT NOT NULL,
        command_id INTEGER NOT NULL, prompt_id INTEGER, target_folder TEXT)""")

    store.db.execute("""CREATE TABLE IF NOT EXISTS folder_name_inputs (
        owner INTEGER NOT NULL, message_id INTEGER NOT NULL, transcript TEXT,
        name TEXT, folder_id TEXT, PRIMARY KEY(owner,message_id)
    )""")
    columns = {r[1] for r in store.db.execute("PRAGMA table_info(folder_name_inputs)")}
    if "rename_id" not in columns:
        store.db.execute("ALTER TABLE folder_name_inputs ADD COLUMN rename_id TEXT")

    store.db.execute("CREATE TABLE IF NOT EXISTS folder_options ("
                          "owner INTEGER PRIMARY KEY, folder_id TEXT NOT NULL)")


def folder_page_settings(store):
    store.db.execute("""CREATE TABLE IF NOT EXISTS user_view_settings (
        owner INTEGER PRIMARY KEY REFERENCES users(id),
        page_size INTEGER NOT NULL DEFAULT 20 CHECK(page_size IN (10, 20, 50, 100))
    )""")
    # Page offsets from the previous eight-record view no longer identify the same records.
    store.db.execute("UPDATE folder_views SET page=0")


def ai_instruction_prompts(store):
    store.db.execute("""CREATE TABLE IF NOT EXISTS ai_instruction_prompts (
        conversation_id TEXT PRIMARY KEY, owner INTEGER NOT NULL,
        message_id INTEGER NOT NULL, reply_to INTEGER
    )""")


def obsidian_devices(store):
    execute_script(store.db, """
        CREATE TABLE IF NOT EXISTS obsidian_pairing (
            code_hash TEXT PRIMARY KEY, owner INTEGER NOT NULL REFERENCES users(id),
            folder_id TEXT, expires_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS obsidian_devices (
            id TEXT PRIMARY KEY, owner INTEGER NOT NULL REFERENCES users(id),
            folder_id TEXT, token_hash TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            created_at REAL NOT NULL, last_used REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS obsidian_device_owner ON obsidian_devices(owner);
    """)


def trash_folders(store):
    # Add only schema here; runtime registration creates each owner's system folder.
    if 'is_trash' not in {r['name'] for r in store.all('PRAGMA table_info(folders)')}:
        store.db.execute("ALTER TABLE folders ADD COLUMN is_trash INTEGER NOT NULL DEFAULT 0 "
                         "CHECK(is_trash IN (0, 1))")
    store.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_trash_per_owner "
                     "ON folders(owner) WHERE is_trash=1")


MIGRATIONS = (baseline, folder_page_settings, ai_instruction_prompts, obsidian_devices, trash_folders)
SCHEMA_VERSION = len(MIGRATIONS)


def migrate(store):
    version = store.db.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError("archive_schema_newer_than_application")
    for number, migration in enumerate(MIGRATIONS, 1):
        if number > version:
            with store.transaction():
                migration(store)
                store.db.execute(f"PRAGMA user_version={number}")


def recover_runtime(store):
    with store.transaction():
        store.db.execute("UPDATE notes SET state='queued' WHERE state='processing' AND deleted=0")
        store.db.execute("UPDATE notes SET delivery='unknown' WHERE delivery='sending'")
        store.db.execute("UPDATE notes SET transcript_delivery='unknown' WHERE transcript_delivery='sending'")
        store.db.execute("UPDATE updates SET state='pending' WHERE state='processing'")
        store.db.execute("UPDATE ai_requests SET state='queued' WHERE state='running'")
        store.db.execute("UPDATE ai_requests SET delivery='unknown' WHERE delivery='sending'")
        store.db.execute("UPDATE note_moves SET state='unknown' WHERE state='publishing'")
        # Retired Telegram-only cleanup must never touch new archive entries.
        store.db.execute("DELETE FROM deletion_targets WHERE owner IN "
                         "(SELECT owner FROM deletion_jobs WHERE files_pending=0)")
        store.db.execute("DELETE FROM deletion_jobs WHERE files_pending=0")
