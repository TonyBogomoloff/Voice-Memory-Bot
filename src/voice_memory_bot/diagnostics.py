"""Read-only operational summary; never migrate, recover or reveal stored text."""
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def inspect_database(path: Path, *, now=None):
    now = time.time() if now is None else now
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def states(table, allowed, column="state", where="1"):
            if table not in tables:
                return None
            counts = dict.fromkeys(allowed, 0)
            counts["other"] = 0
            for state, count in db.execute(f"SELECT {column},count(*) FROM {table} WHERE {where} GROUP BY {column}"):
                counts[state if state in allowed else "other"] += count
            return counts

        def oldest(table, column, where):
            if table not in tables:
                return None
            timestamp = db.execute(f"SELECT min({column}) FROM {table} WHERE {where}").fetchone()[0]
            return max(0, round(now - timestamp)) if timestamp is not None else None

        def count(table, where):
            if table not in tables:
                return None
            return db.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0]

        result = {
            "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
            "missing_tables": sorted({'notes', 'updates', 'ai_requests', 'note_moves', 'trash_cleanup'} - tables),
            "integrity_ok": all(row[0] == "ok" for row in db.execute("PRAGMA quick_check")),
            "foreign_key_violations": sum(1 for _ in db.execute("PRAGMA foreign_key_check")),
            "updates": {"states": states("updates", ("pending", "processing", "failed")),
                        "oldest_created_age_seconds": None, "age_source": "not_recorded"},
            "notes": {"states": states("notes", ("queued", "processing", "ready", "asr_failed"), where="deleted=0"),
                      "oldest_created_age_seconds": oldest("notes", "created_at", "deleted=0 AND state IN ('queued','processing')"),
                      "unknown_delivery": count("notes", "deleted=0 AND (delivery='unknown' OR transcript_delivery='unknown')"),
                      "dirty_archive": count("notes", "deleted=0 AND archive_dirty=1")},
            "ai": {"states": states("ai_requests", ("queued", "running", "ready", "failed", "deleted")),
                   "oldest_created_age_seconds": oldest("ai_requests", "created_at", "state IN ('queued','running')"),
                   "unknown_delivery": count("ai_requests", "state!='deleted' AND delivery='unknown'")},
            "moves": states("note_moves", ("publishing", "cleanup", "done", "unknown")),
            "deletions_pending": count("trash_cleanup", "files_pending=1 OR message_ids!='[]'"),
        }
        result["needs_attention"] = bool(
            result["missing_tables"] or not result["integrity_ok"] or result["foreign_key_violations"]
            or any((counts or {}).get('other') for counts in
                   (result["notes"]["states"], result["updates"]["states"], result["ai"]["states"], result["moves"]))
            or (result["updates"]["states"] or {}).get("failed")
            or (result["notes"]["states"] or {}).get("asr_failed")
            or result["notes"]["unknown_delivery"] or result["ai"]["unknown_delivery"]
            or (result["ai"]["states"] or {}).get("failed")
            or (result["moves"] or {}).get("unknown"))
        return result
