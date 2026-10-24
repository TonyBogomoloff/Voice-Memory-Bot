import asyncio
import json
import time

import httpx
import pytest
from test_mvp import FakeGroq, FakeRouter, FakeTelegram

from voice_memory_bot.bot import HOME, Bot
from voice_memory_bot.config import Config, Limits
from voice_memory_bot.providers import ProviderError, Telegram, TelegramError
from voice_memory_bot.store import Store


def last_message(bot):
    return next(p for m, p in reversed(bot.telegram.sent) if m == "sendMessage")


@pytest.fixture
def bot(tmp_path):
    store = Store(tmp_path / "archive.sqlite3", Limits())
    store.ensure_user(1)
    store.ensure_user(2)
    app = Bot(Config("1:token", frozenset({1, 2}), "key", tmp_path),
              store, FakeTelegram(), FakeGroq(), FakeRouter())
    yield app
    app.store.close()


def note(bot, owner=1, message_id=1001, folder=None, **values):
    folder = folder or bot.store.ensure_user(owner)
    saved, _ = bot.store.add_note(owner, folder, {"message_id": message_id},
                                 "text", transcript="Сохранённый текст", **values)
    bot.archive.persist_text(saved, bot.store.folder(owner, folder))
    return saved


async def confirm(bot, owner=1, thread=None):
    await bot.command(owner, thread, "/delete_all", "")
    token = bot.delete_confirmations[owner][0]
    await bot.callback(owner, thread, 9000, f"confirmdeleteall:{token}")
    return token


def incoming(owner=1, message_id=1001, text="Заметка", thread=None, update_id=1):
    message = {"message_id": message_id, "chat": {"id": owner, "type": "private"},
               "from": {"id": owner}, "text": text}
    if thread:
        message["message_thread_id"] = thread
    return {"update_id": update_id, "message": message}


def test_menu_requires_confirmation_and_cancel_expiry_replay_are_safe(bot):
    async def run():
        old = note(bot)
        assert any(b["callback_data"] == "deleteall" for row in HOME["inline_keyboard"] for b in row)
        await bot.settings_menu(1, None)
        assert "deleteall" in bot.bottom_state(1)["actions"].values()
        await bot.callback(1, None, 500, "deleteall")
        token = bot.delete_confirmations[1][0]
        assert bot.store.note(1, old["id"]) and bot.archive.directory(old).exists()
        with pytest.raises(ValueError, match="устарело"):
            await bot.callback(2, None, 500, f"confirmdeleteall:{token}")
        await bot.callback(1, None, 500, f"canceldeleteall:{token}")
        with pytest.raises(ValueError, match="устарело"):
            await bot.callback(1, None, 500, f"confirmdeleteall:{token}")
        await bot.command(1, None, "/delete_all", "")
        expired = bot.delete_confirmations[1][0]
        bot.delete_confirmations[1] = (expired, time.monotonic() - 1)
        with pytest.raises(ValueError, match="устарело"):
            await bot.callback(1, None, 500, f"confirmdeleteall:{expired}")
        await bot.command(1, None, "/delete_all", "")
        await bot.command(1, None, "/cancel", "")
        assert 1 not in bot.delete_confirmations and bot.store.note(1, old["id"])
        token = await confirm(bot)
        fresh = note(bot, message_id=1002)
        with pytest.raises(ValueError, match="устарело"):
            await bot.callback(1, None, 500, f"confirmdeleteall:{token}")
        assert bot.store.note(1, fresh["id"])
    asyncio.run(run())


def test_full_cleanup_removes_only_owner_data_and_all_known_telegram_locations(bot):
    async def run():
        folder = bot.store.create_folder(1, "Проект", 110)
        bot.store.select(1, folder["id"])
        own = note(bot, folder=folder["id"])
        bot.store.update_note(1, own["id"], state="ready", result_thread=120, result_message_id=1101,
                              transcript_message_id=1102, status_message_id=1103)
        audio = bot.archive.directory(own) / "voice.ogg"
        audio.write_bytes(b"private audio")
        deleted = note(bot, message_id=1002)
        bot.store.delete(1, deleted["id"])
        other = note(bot, owner=2)
        request = bot.ai_state.enqueue(1, own["id"], "button")
        bot.ai_state.save_command(1, own["id"], 0, {"action": "reply", "message": "private response"})
        bot.ai_state.update_request(1, own["id"], state="ready", response="private response")
        bot.ai_state.link(1, 1104, request["conversation_id"])
        bot.ai_state.active(1, folder["id"])
        with bot.store.db:
            bot.ai_state._snapshot(own, own["id"])
        bot.messages.record(own, 1106, 160, "photo")
        bot.messages.begin(own, folder["id"], 160)
        bot.messages.record(other, 2106, 260, "photo")
        (bot.archive.directory(own) / "photo.jpg").write_bytes(b"private photo")
        bot.store.mark_implicit_topic(1, 130)
        bot.cleanup.remember(1, 1105, 140)
        bot.cleanup.remember(2, 1105, 140)
        bot.store.set_export_format(1, "markdown")
        bot.ai_state.set_settings(1, model="saved-model")
        bot.store.reserve_asr(1, 20)
        bot.store.set_action(1, "create")
        bot.store.ingest([incoming(message_id=1003, thread=150, update_id=5),
                          incoming(owner=2, message_id=1003, update_id=6)], {1, 2})
        bot.store.fail_update(5)
        await confirm(bot, thread=110)
        assert bot.store.folders(1) == []
        assert bot.store.export_notes(1) == []
        for table in ("ai_requests", "ai_conversations", "ai_active", "ai_links", "implicit_topics", "note_moves", "note_messages"):
            assert bot.store.all(f"SELECT * FROM {table} WHERE owner=1") == []
        for table in ("note_revisions", "ai_steps"):
            assert bot.store.all(f"SELECT * FROM {table}") == []
        assert bot.store.one("SELECT * FROM users WHERE id=1") == {"id": 1, "active_folder": None, "action": None}
        assert not (bot.archive.root / "1").exists()
        assert bot.archive.directory(other).exists() and bot.store.note(2, other["id"])
        assert bot.store.one("SELECT * FROM telegram_messages WHERE owner=2 AND message_id=1105")
        assert bot.store.one("SELECT 1 FROM note_messages WHERE owner=2 AND message_id=2106")
        assert bot.store.export_format(1) == "markdown"
        assert bot.store.one("SELECT model FROM ai_settings WHERE owner=1")["model"] == "saved-model"
        assert bot.store.one("SELECT seconds FROM asr_usage WHERE owner=1")["seconds"] == 20
        assert bot.store.one("SELECT * FROM updates WHERE id=5") is None
        assert bot.store.one("SELECT * FROM updates WHERE id=6") is not None
        assert bot.store.offset() == 7 and bot.store.all("PRAGMA foreign_key_check") == []
        deleted_topics = {p["message_thread_id"] for m, p in bot.telegram.sent if m == "deleteForumTopic"}
        assert deleted_topics == {110, 120, 130, 140, 150, 160}
        deleted_messages = {mid for m, p in bot.telegram.sent if m == "deleteMessages" for mid in p["message_ids"]}
        assert {1001, 1002, 1102, 1103, 1104} <= deleted_messages
        assert 1101 not in deleted_messages and 1105 not in deleted_messages
        assert all(p["chat_id"] == 1 for m, p in bot.telegram.sent if m.startswith("delete"))
        assert bot.cleanup.job(1) is None and bot.store.claim_note()["id"] == other["id"]
        assert bot.ai_state.claim() is None
        assert last_message(bot)["thread"] is None
        await bot.handle(incoming(message_id=2000, text="/folders"))
        assert bot.store.folders(1) == []
        assert "первую папку" in last_message(bot)["text"]
    asyncio.run(run())


@pytest.mark.parametrize("busy", ["processing", "delivery", "transcript", "ai", "export", "note_lock", "ai_lock"])
def test_cleanup_waits_for_active_work_without_erasing_data(bot, busy):
    async def run():
        saved = note(bot)
        lock = None
        if busy == "processing":
            bot.store.claim_note()
        elif busy == "delivery":
            bot.store.update_note(1, saved["id"], delivery="sending")
        elif busy == "transcript":
            bot.store.update_note(1, saved["id"], transcript_delivery="sending")
        elif busy == "ai":
            bot.ai_state.enqueue(1, saved["id"], "button")
            bot.ai_state.update_request(1, saved["id"], state="running")
        else:
            lock = {"export": bot.export_locks[1], "note_lock": bot.note_locks[saved["id"]],
                    "ai_lock": bot.ai_delivery_locks[saved["id"]]}[busy]
            await lock.acquire()
        try:
            with pytest.raises(ValueError, match="Дождись"):
                await confirm(bot)
            assert bot.store.note(1, saved["id"]) and bot.archive.directory(saved).exists()
            assert bot.cleanup.job(1) is None
            assert not any(method.startswith("delete") for method, _ in bot.telegram.sent)
        finally:
            if lock:
                lock.release()
    asyncio.run(run())


def test_partial_telegram_cleanup_retries_snapshot_after_restart_without_erasing_new_notes(bot, monkeypatch):
    async def run():
        note(bot)
        for mid in range(2000, 2205):
            bot.cleanup.remember(1, mid)
        original = bot.telegram.call
        async def limited(method, **payload):
            if method == "deleteMessages" and 1001 in payload["message_ids"]:
                raise TelegramError(400, method, "message can't be deleted")
            if method == "deleteMessage" and payload["message_id"] == 1001:
                raise TelegramError(400, method, "message can't be deleted")
            return await original(method, **payload)
        monkeypatch.setattr(bot.telegram, "call", limited)
        await confirm(bot)
        assert [t["identity"] for t in bot.cleanup.targets(1, "message")] == [1001]
        assert "сообщений — 1" in last_message(bot)["text"]
        assert all(1 <= len(p["message_ids"]) <= 100 for m, p in bot.telegram.sent if m == "deleteMessages")
        fresh = note(bot, message_id=5000)
        bot.store.close()
        bot.store = Store(bot.config.data_dir / "archive.sqlite3", Limits())
        resumed = Bot(bot.config, bot.store, FakeTelegram(), FakeGroq(), FakeRouter())
        await resumed.resume_deletions()
        assert resumed.cleanup.job(1) is None
        assert resumed.store.note(1, fresh["id"]) and resumed.archive.directory(fresh).exists()
        batches = [p["message_ids"] for m, p in resumed.telegram.sent if m == "deleteMessages"]
        assert batches == [[1001]]
        assert "Новые записи в архиве сохранены" in last_message(resumed)["text"]
    asyncio.run(run())


def test_interrupted_file_cleanup_blocks_new_notes_and_resumes_from_durable_intent(bot, monkeypatch):
    async def run():
        old = note(bot)
        other = note(bot, owner=2)
        original = bot.archive.delete_all
        def disk_failure(owner):
            raise OSError("disk unavailable")
        monkeypatch.setattr(bot.archive, "delete_all", disk_failure)
        await confirm(bot)
        assert bot.cleanup.job(1)["files_pending"] == 1
        assert bot.store.export_notes(1) == [] and bot.archive.directory(old).exists()
        await bot.handle(incoming(message_id=5000))
        assert bot.store.export_notes(1) == []
        assert "Очистка ещё не завершена" in last_message(bot)["text"]
        monkeypatch.setattr(bot.archive, "delete_all", original)
        await bot.handle({"callback_query": {"id": "retry", "from": {"id": 1}, "data": "retrydeleteall",
                                             "message": incoming(message_id=5001)["message"]}})
        assert not bot.archive.directory(old).exists() and bot.archive.directory(other).exists()
        assert bot.cleanup.job(1) is None
    asyncio.run(run())


def test_transport_failure_keeps_topic_and_messages_until_confirmed_or_already_missing(bot, monkeypatch):
    async def run():
        folder = bot.store.create_folder(1, "Проект", 110)
        note(bot, folder=folder["id"])
        original = bot.telegram.call
        async def unavailable(method, **payload):
            if method == "deleteForumTopic":
                raise ProviderError("telegram_transport_unknown")
            return await original(method, **payload)
        monkeypatch.setattr(bot.telegram, "call", unavailable)
        await confirm(bot)
        assert bot.cleanup.targets(1, "topic") and bot.cleanup.targets(1, "message")
        assert "не подтвердил" in last_message(bot)["text"]
        await bot.handle(incoming(message_id=2000, thread=110))
        assert bot.store.folders(1) == [] and bot.store.export_notes(1) == []
        assert "Эта тема ожидает удаления" in last_message(bot)["text"]
        async def already_missing(method, **payload):
            if method == "deleteForumTopic":
                raise TelegramError(400, method, "message thread not found")
            return await original(method, **payload)
        monkeypatch.setattr(bot.telegram, "call", already_missing)
        await bot.callback(1, None, 500, "retrydeleteall")
        assert bot.cleanup.job(1) is None
    asyncio.run(run())


def test_inflight_status_cleanup_and_scheduled_export_do_not_restore_deleted_data(bot, monkeypatch):
    async def run():
        saved = note(bot)
        bot.store.update_note(1, saved["id"], state="ready", status_message_id=3000)
        entered, finish = asyncio.Event(), asyncio.Event()
        original = bot.telegram.call
        async def paused(method, **payload):
            if method == "deleteMessage" and payload["message_id"] == 3000:
                entered.set()
                await finish.wait()
            return await original(method, **payload)
        monkeypatch.setattr(bot.telegram, "call", paused)
        status = asyncio.create_task(bot.clear_status(saved))
        await asyncio.wait_for(entered.wait(), 1)
        await confirm(bot)
        finish.set()
        await status
        assert bot.store.export_notes(1) == []
        bot.deleting_owners.add(1)
        await bot.send_export(1, None, None, "all")
        assert not any("Готовлю ZIP" in str(p) for _, p in bot.telegram.sent)
        bot.deleting_owners.clear()
    asyncio.run(run())


def test_telegram_tracks_every_sent_chunk_media_and_upload_without_contents(tmp_path):
    async def run():
        mid = 100
        def handler(request):
            nonlocal mid
            mid += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": mid}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            telegram = Telegram("1:token", client)
            recorded = []
            telegram.on_message = lambda *args: recorded.append(args)
            await telegram.text(1, "private text", 20)
            await telegram.call("sendVoice", chat_id=1, message_thread_id=20, voice="private file id")
            path = tmp_path / "archive.zip"
            path.write_bytes(b"private content")
            await telegram.document(1, path, 20)
            assert recorded == [(1, 101, 20), (1, 102, 20), (1, 103, 20)]
            assert "private" not in json.dumps(recorded)
    asyncio.run(run())
