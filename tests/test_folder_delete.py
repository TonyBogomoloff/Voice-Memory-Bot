import asyncio
import time

import pytest
from test_mvp import make_bot, receive_voice
from test_mvp import store as store


def test_folder_deletion_keeps_files_notes_versions_ai_history_and_other_users(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        folder = store.create_folder(1, "Проект", 100)
        other = store.create_folder(2, "Проект", 200)
        store.select(1, folder["id"])
        await receive_voice(bot)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        bot.ai_state._snapshot(note)
        conversation = bot.ai_state.active(1, folder["id"])
        original = bot.archive.audio(note).read_bytes()
        transcript = note["transcript"]
        bot.telegram.sent.clear()
        inbox = store.delete_folder(1, folder["id"])
        bot.flush_ai_archive()
        saved = store.note(1, note["id"])
        assert saved["folder_id"] == saved["source_folder"] == store.current_folder(1) == inbox
        assert saved["transcript"] == transcript and bot.archive.audio(saved).read_bytes() == original
        assert bot.ai_state.conversation(1, conversation)["folder_id"] == inbox
        assert store.one("SELECT folder_id FROM note_revisions WHERE note_id=?", (note["id"],))["folder_id"] == inbox
        assert store.folder(2, other["id"])["thread_id"] == 200
        assert store.is_implicit_topic(1, 100)
        assert store.all("PRAGMA foreign_key_check") == []
        assert store.one("PRAGMA quick_check")["quick_check"] == "ok"
        assert "Проект: Входящие" in (bot.archive.directory(saved) / "note.md").read_text()
        assert bot.telegram.sent == []
    asyncio.run(run())


def test_delete_folder_confirmation_is_owner_scoped_cancelable_expiring_and_blocks_busy_work(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        folder = store.create_folder(1, "Работа")
        other = store.create_folder(2, "Личное")
        await bot.callback(1, None, 500, f"dropfolder:{folder['id']}")
        token = bot.folder_delete_confirmations[1][0]
        assert store.folder(1, folder["id"])
        with pytest.raises(ValueError, match="устарело"):
            await bot.callback(2, None, 500, f"confirmdrop:{token}")
        await bot.callback(1, None, 500, f"canceldrop:{token}")
        with pytest.raises(ValueError):
            await bot.callback(1, None, 500, f"confirmdrop:{token}")
        with pytest.raises(ValueError):
            store.delete_folder(1, store.ensure_user(1))
        with pytest.raises(ValueError):
            store.delete_folder(1, other["id"])
        await bot.callback(1, None, 500, f"dropfolder:{folder['id']}")
        token, identity, name, _ = bot.folder_delete_confirmations[1]
        bot.folder_delete_confirmations[1] = (token, identity, name, time.monotonic() - 1)
        with pytest.raises(ValueError, match="устарело"):
            await bot.callback(1, None, 500, f"confirmdrop:{token}")
        note, _ = store.add_note(1, folder["id"], {"message_id": 10}, "text", transcript="Сохранить")
        with pytest.raises(ValueError, match="завершения"):
            store.delete_folder(1, folder["id"])
        store.update_note(1, note["id"], state="ready", delivery="sent")
        await bot.callback(1, None, 500, f"dropfolder:{folder['id']}")
        token = bot.folder_delete_confirmations[1][0]
        await bot.callback(1, None, 500, f"confirmdrop:{token}")
        with pytest.raises(ValueError):
            store.folder(1, folder["id"])
        assert store.note(1, note["id"])["folder_id"] == store.ensure_user(1)
        with pytest.raises(ValueError):
            await bot.callback(1, None, 500, f"confirmdrop:{token}")
    asyncio.run(run())
