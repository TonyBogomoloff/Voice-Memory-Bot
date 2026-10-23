"""Additional metadata for browser uploads in the existing private archive."""


def migrate_web(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(notes)")}
    with db:
        for name, declaration in {
            "origin": "TEXT NOT NULL DEFAULT 'telegram'",
            "original_name": "TEXT",
            "mime_type": "TEXT",
            "upload_key": "TEXT",
        }.items():
            if name not in columns:
                db.execute(f"ALTER TABLE notes ADD COLUMN {name} {declaration}")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS web_upload_key ON notes(owner,upload_key) "
                   "WHERE upload_key IS NOT NULL")
