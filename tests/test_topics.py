import asyncio

import httpx
import pytest
from test_mvp import make_bot, receive_voice
from test_mvp import store as store

from voice_memory_bot.providers import Telegram


def test_custom_folder_wins_over_legacy_telegram_topic_and_spoken_folder(tmp_path, store):
    async def run():
        store.create_folder(1, "Test1", thread=100)
        second = store.create_folder(1, "Test2", thread=200)
        store.select(1, second["id"])
        bot = make_bot(tmp_path, store)
        async def transcript(_):
            return "Сохрани в папку Test2. Ещё одна заметка."
        async def classifier(*args):
            raise AssertionError("Folder routing must not depend on an LLM")
        bot.groq.transcribe, bot.router.classify = transcript, classifier
        for message_id, thread, folder in [(10, 100, second), (20, 200, second), (30, 100, second)]:
            bot.telegram.sent.clear()
            await receive_voice(bot, message_id, thread)
            await bot.process(store.claim_note())
            note = store.one("SELECT * FROM notes WHERE message_id=?", (message_id,))
            assert note["folder_id"] == folder["id"] and note["result_thread"] == thread
            assert note["delivery"] == "sent"
            assert [m for m, _ in bot.telegram.sent] == ["sendMessage", "deleteMessage", "sendMessage"]
            assert bot.telegram.sent[-1][1]["thread"] == thread
    asyncio.run(run())


def test_old_topic_metadata_never_imports_a_new_folder(tmp_path, store):
    async def run():
        store.mark_implicit_topic(1, 100)
        bot = make_bot(tmp_path, store)
        await bot.handle({"message": {"message_id": 10, "from": {"id": 1},
            "chat": {"id": 1, "type": "private"}, "message_thread_id": 100,
            "text": "Запись в выбранной папке",
            "reply_to_message": {"message_id": 1, "message_thread_id": 100,
                                 "forum_topic_created": {"name": "Test1", "is_name_implicit": True}}}})
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        assert note["folder_id"] == store.ensure_user(1)
        assert store.folder_for_thread(1, 100) is None
        assert note["source_thread"] == note["result_thread"] == 100
        assert not any(m == "createForumTopic" for m, _ in bot.telegram.sent)
    asyncio.run(run())


@pytest.mark.parametrize("recover_at", ["status", "transcript", "never"])
def test_missing_topic_replies_to_source_and_uses_only_confirmed_topic(tmp_path, store, recover_at):
    async def run():
        store.create_folder(1, "Test1", 100)
        other = store.create_folder(1, "Test2", 200)
        store.select(1, other["id"])
        bot = make_bot(tmp_path, store)
        # Telegram message IDs must not collide with the incoming message in this fixture.
        bot.telegram.message_sequence = 1000
        call = bot.telegram.call
        async def receipt(method, **payload):
            result = await call(method, **payload)
            if method == "sendMessage":
                is_status = payload["text"] == "Конвертирую в текст…"
                if recover_at != "never" and (recover_at == "status" or not is_status):
                    result["message_thread_id"] = 100
            return result
        bot.telegram.call = receipt
        await receive_voice(bot, thread=None)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        assert note["folder_id"] == other["id"]
        assert note["source_thread"] == note["result_thread"] == (None if recover_at == "never" else 100)
        sends = [p for m, p in bot.telegram.sent if m == "sendMessage"]
        assert len(sends) == 2 and sends[0]["reply_to"] == 10
        assert sends[1]["reply_to"] == (None if recover_at == "status" else 10)
        assert note["delivery"] == "sent"
        assert not any(m in {"sendVoice", "createForumTopic"} for m, _ in bot.telegram.sent)
        assert 10 in bot.messages.in_thread(1, note["id"], note["source_thread"])
    asyncio.run(run())


def test_missing_topic_reply_cannot_fall_back_to_an_unrelated_chat_position():
    async def run():
        payloads = []
        def handle(request):
            import json
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 12}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            api = Telegram("123:secret", client=client)
            await api.text(1, "Расшифровка", reply_to=10)
        assert payloads[0]["reply_parameters"] == {"message_id": 10, "allow_sending_without_reply": False}
        assert "message_thread_id" not in payloads[0]
    asyncio.run(run())
