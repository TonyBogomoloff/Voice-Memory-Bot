import asyncio

import pytest
from support.base import make_bot, receive_voice

from voice_memory_bot.providers import TelegramError


@pytest.mark.parametrize("blocked", [False, True])
def test_delete_removes_voice_and_all_transcript_parts(tmp_path, store, blocked):
    async def run():
        bot = make_bot(tmp_path, store)
        bot.telegram.message_sequence = 1000
        await receive_voice(bot, 100, 200)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        # A long transcript or a re-opened note can have multiple known messages.
        bot.messages.record(note, 2000, 200, "transcript")
        expected = {r["message_id"] for r in store.all(
            "SELECT message_id FROM note_messages WHERE note_id=? AND deleted=0", (note["id"],))}
        assert {100, note["transcript_message_id"], 2000} <= expected
        other, _ = store.add_note(2, store.ensure_user(2), {"message_id": 3000},
                                  "text", transcript="Чужая запись")
        bot.messages.record(other, 3001, None, "transcript")
        path = bot.archive.audio(note)
        assert path.exists()
        await bot.callback(1, 200, note["transcript_message_id"], f"delete:{note['id']}")
        assert not store.note(1, note["id"])["deleted"]
        bot.telegram.sent.clear()
        original = bot.telegram.call

        async def call(method, **kwargs):
            if blocked and method == "deleteMessage" and kwargs["message_id"] == 100:
                bot.telegram.sent.append((method, kwargs))
                raise TelegramError(400, method, "message can't be deleted")
            return await original(method, **kwargs)

        bot.telegram.call = call
        await bot.callback(1, 200, 5000, f"confirmdelete:{note['id']}")
        deleted = {p["message_id"] for m, p in bot.telegram.sent if m == "deleteMessage"}
        assert expected <= deleted
        assert not ({3000, 3001} & deleted)
        assert store.one("SELECT deleted FROM notes WHERE id=?", (note["id"],))["deleted"] == 1
        assert not path.exists()
        assert store.note(2, other["id"])["transcript"] == "Чужая запись"
        assert store.one("SELECT deleted FROM note_messages WHERE owner=1 AND message_id=2000")["deleted"] == 1
        texts = [p["text"] for m, p in bot.telegram.sent if m == "sendMessage"]
        assert any(("не удалось" if blocked else "Сообщение удалено.") in text for text in texts)

    asyncio.run(run())
