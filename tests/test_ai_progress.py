import asyncio

import pytest
from test_ai import bot as bot
from test_ai import ready_note, receive, step
from test_ai_note_actions import request_for

from voice_memory_bot.ai_store import AIStore
from voice_memory_bot.providers import ProviderError, TelegramError


@pytest.mark.parametrize("failure", [False, True])
def test_voice_reply_shows_progress_before_model_and_edits_it(bot, failure):
    async def run():
        note = await receive(bot, voice=True, reply=777)
        request = bot.ai_state.request(1, note["id"])
        progress = bot.store.one("SELECT message_id FROM ai_progress")["message_id"]
        assert not bot.assistant.calls
        assert bot.telegram.sent[-1][1]["text"] == "🤖 Обрабатываю команду…"
        assert bot.telegram.sent[-1][1]["reply_to"] == note["transcript_message_id"]
        # Durable ID survives recovery and never causes a second status send.
        AIStore(bot.store)
        count = len(bot.telegram.sent)
        await bot.show_ai_progress(request)
        assert len(bot.telegram.sent) == count
        bot.assistant.responses = [ProviderError("offline") if failure else step(message="Решение")]
        await bot.process_ai(bot.ai_state.claim())
        method, payload = bot.telegram.sent[-1]
        assert method == "editMessageText" and payload["message_id"] == progress
        assert "Обрабатываю" not in payload["text"]
        assert bool(payload["reply_markup"]["inline_keyboard"]) is failure
        result = bot.ai_state.request(1, note["id"])
        assert result["delivery"] == "sent"
        assert bool(result["result_note_id"]) is not failure
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["message to edit not found", "message is not modified", "network"])
def test_progress_edit_errors_do_not_duplicate_uncertain_delivery(bot, reason):
    async def run():
        note = await receive(bot, voice=True, reply=777)
        original = bot.telegram.call
        async def call(method, **kwargs):
            if method == "editMessageText":
                raise ProviderError("network") if reason == "network" else TelegramError(400, method, reason)
            return await original(method, **kwargs)
        bot.telegram.call = call
        count = len(bot.telegram.sent)
        await bot.process_ai(bot.ai_state.claim())
        current = bot.ai_state.request(1, note["id"])
        assert current["delivery"] == ("unknown" if reason == "network" else "sent")
        assert len(bot.telegram.sent) - count == (1 if reason == "message to edit not found" else 0)
        if reason == "message to edit not found":
            assert bot.telegram.sent[-1][1]["reply_to"] == note["transcript_message_id"]
    asyncio.run(run())


def test_long_reply_edits_first_chunk_and_keeps_controls_on_last(bot):
    async def run():
        note = await receive(bot, voice=True, reply=777)
        count = len(bot.telegram.sent)
        bot.assistant.responses = [step(message="Длинный ответ. " * 500)]
        await bot.process_ai(bot.ai_state.claim())
        messages = bot.telegram.sent[count:]
        assert messages[0][0] == "editMessageText"
        assert messages[-1][0] == "sendMessage" and messages[-1][1]["keyboard"]
        assert messages[-1][1]["reply_to"] == note["transcript_message_id"]
    asyncio.run(run())


@pytest.mark.parametrize("action", ["move_note", "delete_note"])
def test_command_confirmation_is_delivered_but_not_archived_even_after_restart(bot, action):
    async def run():
        note = ready_note(bot)
        request = request_for(bot, note)
        folder = bot.store.create_folder(1, "Работа")
        bot.assistant.responses = [step("read_note", note_id=note["id"]),
            step(action, note_id=note["id"], revision=0, folder_id=folder["id"]), step(message="Готово")]
        await bot.process_ai(bot.ai_state.claim())
        current = bot.ai_state.request(1, request)
        assert current["response"] == "Готово" and current["delivery"] == "sent"
        assert current["result_note_id"] is None
        assert "Готово" in bot.telegram.sent[-1][1]["text"]
        AIStore(bot.store).archive_replies()
        assert bot.ai_state.request(1, request)["result_note_id"] is None
        assert not bot.store.one("SELECT id FROM notes WHERE ai_generated_from=?", (request,))
    asyncio.run(run())
