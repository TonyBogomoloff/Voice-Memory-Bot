import asyncio

import pytest
from test_mvp import make_bot, receive_voice
from test_mvp import store as store

from voice_memory_bot.config import Limits
from voice_memory_bot.store import Store


def test_move_preserves_archive_deletes_source_receipts_and_can_move_back(tmp_path, store):
    async def run():
        first = store.create_folder(1, "Игра", 100)
        target = store.create_folder(1, "Работа", 200)
        store.select(1, first["id"])
        bot = make_bot(tmp_path, store)
        bot.telegram.message_sequence = 1000
        await receive_voice(bot)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        receipts = bot.messages.in_thread(1, note["id"], 100)
        original = bot.archive.audio(note).read_bytes()
        bot.telegram.sent.clear()
        bot.telegram.fail_delivery = True
        await bot.move(1, note["id"], target["id"], 100)
        bot.flush_ai_archive()
        saved = store.note(1, note["id"])
        assert saved["folder_id"] == target["id"] and saved["revision"] == note["revision"] + 1
        assert saved["transcript"] == note["transcript"]
        assert bot.archive.audio(saved).read_bytes() == original
        assert bot.messages.in_thread(1, note["id"], 100) == []
        assert [p["message_id"] for m, p in bot.telegram.sent if m == "deleteMessage"] == receipts
        assert bot.messages.get(1, note["id"])["state"] == "done"
        bot.telegram.sent.clear()
        await bot.move(1, note["id"], first["id"], 200)
        assert store.note(1, note["id"])["folder_id"] == first["id"]
        assert bot.telegram.sent == []
        assert store.current_folder(1) == first["id"]
        with pytest.raises(ValueError):
            await bot.move(1, note["id"], store.ensure_user(2), None)
        async with bot.note_locks[note["id"]]:
            with pytest.raises(ValueError, match="завершения"):
                await bot.move(1, note["id"], target["id"], None)
    asyncio.run(run())


def test_legacy_confirmed_move_cleanup_resumes_without_republishing(tmp_path, store):
    async def run():
        target = store.create_folder(1, "Работа", 200)
        bot = make_bot(tmp_path, store)
        await receive_voice(bot)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        source_ids = bot.messages.in_thread(1, note["id"], 100)
        # A receipt left by the old release after its destination was confirmed.
        bot.messages.begin(note, target["id"], 100)
        bot.messages.confirm(note, target["id"], {"message_id": 700}, 200)
        store.close()
        restarted = Store(tmp_path / "archive.sqlite3", Limits())
        try:
            bot = make_bot(tmp_path, restarted)
            task = asyncio.create_task(bot.worker())
            try:
                async with asyncio.timeout(2):
                    while bot.messages.get(1, note["id"])["state"] != "done":
                        await asyncio.sleep(.01)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert [p["message_id"] for m, p in bot.telegram.sent if m == "deleteMessage"] == source_ids
            assert not any(m.startswith("send") for m, _ in bot.telegram.sent)
        finally:
            restarted.close()
    asyncio.run(run())


def test_move_picker_filters_source_before_pagination(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        source = store.current_folder(1)
        note, _ = store.add_note(1, source, {"message_id": 50}, "text", transcript="Текст")
        targets = [store.create_folder(1, f"Папка {i}")["id"] for i in range(8)]
        await bot.choose_note_folder(1, note["id"], 50, 0)
        actions = bot.bottom_state(1)["actions"].values()
        assert {a.split(":")[-1] for a in actions if a.startswith("rmove:")} == set(targets)
        assert not any(a.startswith("rpage:") for a in actions)
        await bot.show_folders(1, note_id=note["id"])
        actions = bot.bottom_state(1)["actions"].values()
        assert {a.split(":")[-1] for a in actions if a.startswith("rmove:")} == set(targets)
        for target in targets:
            store.db.execute("UPDATE folders SET removed=1 WHERE id=?", (target,))
        store.db.commit()
        await bot.choose_note_folder(1, note["id"], 50, 0)
        assert set(bot.bottom_state(1)["actions"]) == {"＋ Новая папка", "Отмена"}
    asyncio.run(run())


def test_move_cleanup_retries_after_restart_without_deleting_new_view(tmp_path, store):
    async def run():
        from voice_memory_bot.providers import ProviderError
        bot = make_bot(tmp_path, store)
        await receive_voice(bot)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        target = store.create_folder(1, "Работа")
        bot.messages.record(note, 9000, None, "view")
        bot.cleanup.remember(1, 9000)
        original_call = bot.telegram.call

        async def fail(method, **payload):
            if method == "deleteMessage":
                assert store.note(1, note["id"])["folder_id"] == target["id"]
                raise ProviderError("telegram_transport_unknown")
            return await original_call(method, **payload)

        bot.telegram.call = fail
        await bot.move(1, note["id"], target["id"], None)
        assert bot.messages.get(1, note["id"])["state"] == "cleanup"
        store.close()
        restarted = Store(tmp_path / "archive.sqlite3", Limits())
        try:
            bot = make_bot(tmp_path, restarted)
            bot.messages.record(restarted.note(1, note["id"]), 9001, None, "view")
            await bot.cleanup_move(bot.messages.pending())
            deleted = {p["message_id"] for m, p in bot.telegram.sent if m == "deleteMessage"}
            assert 9000 in deleted and 9001 not in deleted
            assert bot.messages.get(1, note["id"])["state"] == "done"
            assert not restarted.one("SELECT 1 FROM telegram_messages WHERE owner=1 AND message_id=9000")
        finally:
            restarted.close()
    asyncio.run(run())


def test_move_keeps_original_when_media_is_not_archived(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        await receive_voice(bot)
        note = store.notes(1)[0]
        store.update_note(1, note["id"], state="asr_failed")
        target = store.create_folder(1, "Работа")
        bot.telegram.sent.clear()
        with pytest.raises(ValueError, match="сохранение файла"):
            await bot.move(1, note["id"], target["id"], 100)
        assert store.note(1, note["id"])["folder_id"] == note["folder_id"]
        assert bot.telegram.sent == []
    asyncio.run(run())
