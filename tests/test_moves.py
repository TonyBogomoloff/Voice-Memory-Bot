import asyncio

import pytest
from test_mvp import make_bot, receive_voice
from test_mvp import store as store

from voice_memory_bot.config import Limits
from voice_memory_bot.providers import ProviderError, TelegramError
from voice_memory_bot.store import Store


def test_move_removes_all_source_parts_and_can_move_back(tmp_path, store):
    async def run():
        game = store.create_folder(1, "Игра", thread=100)
        work = store.create_folder(1, "Работа", thread=200)
        bot = make_bot(tmp_path, store)
        bot.telegram.message_sequence = 1000
        async def long_transcript(_):
            return "Сохрани в проект Игра. " + "Длинная мысль 🚀. " * 800
        bot.groq.transcribe = long_transcript
        await receive_voice(bot)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        source_ids = bot.messages.in_thread(1, note["id"], 100)
        assert len(source_ids) > 3 and 10 in source_ids
        other, _ = store.add_note(1, game["id"], {"message_id": 99, "message_thread_id": 100},
                                 "text", transcript="Другую запись не удалять")
        bot.messages.record(other, 99, 100, "source")
        bot.telegram.sent.clear()
        await bot.move(1, note["id"], work["id"], 100)
        deleted = [p["message_id"] for m, p in bot.telegram.sent if m == "deleteMessage"]
        assert deleted == source_ids and 99 not in deleted
        first_delete = next(i for i, (m, _) in enumerate(bot.telegram.sent) if m == "deleteMessage")
        assert all(m.startswith("send") for m, _ in bot.telegram.sent[:first_delete])
        destination_ids = bot.messages.in_thread(1, note["id"], 200)
        assert len(destination_ids) == len(source_ids)
        assert bot.telegram.sent[-1][1]["text"] == "Запись перенесена."
        bot.telegram.sent.clear()
        await bot.move(1, note["id"], game["id"], 200)
        deleted = [p["message_id"] for m, p in bot.telegram.sent if m == "deleteMessage"]
        assert deleted == destination_ids
        returned = bot.messages.in_thread(1, note["id"], 100)
        assert len(returned) == len(source_ids) and not set(returned) & set(source_ids)
        assert store.note(1, note["id"])["folder_id"] == game["id"]
    asyncio.run(run())


@pytest.mark.parametrize("failed_method", ["sendVoice", "sendMessage"])
def test_move_does_not_delete_source_or_change_folder_on_uncertain_delivery(tmp_path, store, failed_method):
    async def run():
        game = store.create_folder(1, "Игра", thread=100)
        work = store.create_folder(1, "Работа", thread=200)
        bot = make_bot(tmp_path, store)
        await receive_voice(bot)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        call = bot.telegram.call
        async def fail_destination(method, **kwargs):
            if method == failed_method and kwargs.get("message_thread_id", kwargs.get("thread")) == 200:
                raise ProviderError("telegram_transport_unknown")
            return await call(method, **kwargs)
        bot.telegram.call = fail_destination
        bot.telegram.sent.clear()
        await bot.move(1, note["id"], work["id"], 100)
        assert all(m != "deleteMessage" for m, _ in bot.telegram.sent)
        assert store.note(1, note["id"])["folder_id"] == game["id"]
        assert bot.messages.get(1, note["id"])["state"] == "unknown"
        assert "Исходные сообщения сохранены" in bot.telegram.sent[-1][1]["text"]
    asyncio.run(run())


def test_move_cleanup_resumes_after_restart_without_republishing(tmp_path, store):
    async def run():
        store.create_folder(1, "Игра", thread=100)
        work = store.create_folder(1, "Работа", thread=200)
        bot = make_bot(tmp_path, store)
        await receive_voice(bot)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        source_ids = bot.messages.in_thread(1, note["id"], 100)
        call = bot.telegram.call
        async def fail_cleanup(method, **kwargs):
            if method == "deleteMessage":
                raise ProviderError("telegram_transport_unknown")
            return await call(method, **kwargs)
        bot.telegram.call = fail_cleanup
        await bot.move(1, note["id"], work["id"], 100)
        assert bot.messages.get(1, note["id"])["state"] == "cleanup"
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
            assert [p["text"] for m, p in bot.telegram.sent if m == "sendMessage"] == ["Запись перенесена."]
            assert all(m != "sendVoice" for m, _ in bot.telegram.sent)
        finally:
            restarted.close()
    asyncio.run(run())


def test_move_reports_telegram_delete_restriction_without_losing_destination(tmp_path, store):
    async def run():
        store.create_folder(1, "Игра", thread=100)
        work = store.create_folder(1, "Работа", thread=200)
        bot = make_bot(tmp_path, store)
        await receive_voice(bot)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        call = bot.telegram.call
        async def old_source(method, **kwargs):
            if method == "deleteMessage" and kwargs["message_id"] == 10:
                raise TelegramError(400, method, "Bad Request: message can't be deleted")
            return await call(method, **kwargs)
        bot.telegram.call = old_source
        await bot.move(1, note["id"], work["id"], 100)
        assert store.note(1, note["id"])["folder_id"] == work["id"]
        assert bot.messages.in_thread(1, note["id"], 100) == [10]
        assert bot.messages.get(1, note["id"])["state"] == "blocked"
        assert "Telegram не разрешил" in bot.telegram.sent[-1][1]["text"]
    asyncio.run(run())
