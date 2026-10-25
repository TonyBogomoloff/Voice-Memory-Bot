import json
import sqlite3
from pathlib import Path

import pytest
from support.base import add

from voice_memory_bot import cli
from voice_memory_bot.ai_store import AIStore
from voice_memory_bot.diagnostics import inspect_database


def test_diagnostics_is_readonly_preserves_inflight_and_redacts_content(store, monkeypatch, capsys):
    private = "PRIVATE_TRANSCRIPT_TOKEN_AND_ERROR"
    note = add(store, kind="text", transcript=private)
    store.update_note(1, note["id"], state="processing", delivery="unknown", error=private)
    ai = AIStore(store)
    ai.enqueue(1, note["id"], "button")
    ai.update_request(1, note["id"], state="running", delivery="sending")
    with store.transaction():
        store.db.execute("INSERT INTO updates VALUES (1,?,'failed')", (json.dumps({"text": private}),))
        store.db.execute("PRAGMA user_version=0")
    path = store.db.execute("PRAGMA database_list").fetchone()[2]
    dump = list(store.db.iterdump())
    result = inspect_database(Path(path), now=note["created_at"] + 100)
    assert result["notes"]["states"]["processing"] == 1
    assert result["notes"]["oldest_created_age_seconds"] == 100
    assert result["updates"]["states"]["failed"] == 1 and result["needs_attention"]
    assert result["ai"]["states"]["running"] == 1
    assert result["schema_version"] == 0 and list(store.db.iterdump()) == dump
    assert private not in json.dumps(result)
    monkeypatch.setattr('sys.argv', ['voice-memory-bot', 'diagnose', '--database', path,
                                    '--config', '/missing-config-must-not-be-loaded'])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert private not in capsys.readouterr().out and list(store.db.iterdump()) == dump


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / 'absent.sqlite3'
    with pytest.raises(sqlite3.OperationalError):
        inspect_database(path)
    assert not path.exists()
