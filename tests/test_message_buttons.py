import asyncio

import pytest
from test_commands import bot as bot
from test_commands import receive
from test_mvp import make_bot, receive_voice
from test_photos import PHOTO, photo_bot, receive_photo

from voice_memory_bot.store import Store


def test_settings_toggle_is_owner_scoped_persistent_and_not_a_note(bot, tmp_path):
    async def run():
        assert not bot.store.message_buttons_enabled(1)
        await receive(bot, "/settings", 5000)
        assert "Кнопки у сообщений: выкл" in bot.bottom_state(1)["actions"]
        await receive(bot, "Кнопки у сообщений: выкл", 5001)
        assert "Кнопки у сообщений: вкл" in bot.bottom_state(1)["actions"]
        reopened = Store(tmp_path / "archive.sqlite3", bot.store.limits)
        try:
            assert reopened.message_buttons_enabled(1)
            assert not reopened.message_buttons_enabled(2)
            restarted = make_bot(tmp_path, reopened)
            await receive(restarted, "Кнопки у сообщений: вкл", 5002)
            assert not reopened.message_buttons_enabled(1)
            assert restarted.bottom_state(1)["back"] == "home"
            assert reopened.notes(1) == []
        finally:
            reopened.close()
    asyncio.run(run())


@pytest.mark.parametrize("thread", [None, 100])
def test_disabled_text_keeps_exact_original_without_reposting_even_after_restart(bot, tmp_path, thread):
    async def run():
        text = "  Точный текст\nс переносом и ссылкой https://example.com  "
        await receive(bot, text, 5001, thread)
        note = bot.store.notes(1)[0]
        assert bot.telegram.sent == []
        assert note["transcript"] == text
        assert note["transcript_message_id"] == 5001
        assert bot.messages.in_thread(1, note["id"], thread) == [5001]
        assert bot.archive.directory(note).is_dir()
        reopened = Store(tmp_path / "archive.sqlite3", bot.store.limits)
        try:
            restarted = make_bot(tmp_path, reopened)
            reopened.set_message_buttons(1, True)
            await restarted.process(reopened.claim_note())
            saved = reopened.note(1, note["id"])
            assert saved["state"] == "ready" and saved["delivery"] == "sent"
            assert saved["result_message_id"] == 5001
            assert restarted.telegram.sent == []
            assert restarted.messages.in_thread(1, note["id"], thread) == [5001]
        finally:
            reopened.close()
    asyncio.run(run())


@pytest.mark.parametrize("caption", ["", "  Подпись к фото\nбез изменений  "])
def test_disabled_photo_archives_original_without_reposting(bot, tmp_path, caption):
    async def run():
        app = photo_bot(tmp_path, bot.store)
        await receive_photo(app, caption)
        await app.process(app.store.claim_note())
        note = app.store.notes(1)[0]
        assert app.telegram.sent == []
        assert app.archive.audio(note).read_bytes() == PHOTO
        assert note["transcript"] == caption and note["state"] == "ready"
        assert note["delivery"] == note["transcript_delivery"] == "sent"
        assert note["result_message_id"] == note["transcript_message_id"] == 10
        assert app.messages.in_thread(1, note["id"], 100) == [10]
        app.store.set_message_buttons(1, True)
        await app.deliver(note)
        await app.cleanup_photo_source(note)
        assert app.telegram.sent == []
    asyncio.run(run())


@pytest.mark.parametrize("enabled", [False, True])
def test_voice_transcription_and_archive_work_in_both_modes(bot, enabled):
    async def run():
        bot.telegram.message_sequence = 1000
        bot.store.set_message_buttons(1, enabled)
        await receive_voice(bot, message_id=5001)
        await bot.process(bot.store.claim_note())
        note = bot.store.notes(1)[0]
        assert note["state"] == "ready" and note["delivery"] == "sent"
        assert bot.archive.audio(note).is_file()
        assert bot.telegram.sent[-1][1]["text"] == note["transcript"]
        rows = bot.telegram.sent[-1][1]["reply_markup"]["inline_keyboard"]
        assert bool(rows) is enabled
        assert not any(m == "deleteMessage" and p["message_id"] == 5001 for m, p in bot.telegram.sent)
        bot.telegram.sent.clear()
        await bot.show_note(1, note["id"], 100)
        assert bool(bot.telegram.sent[-1][1]["keyboard"]["inline_keyboard"]) is enabled
    asyncio.run(run())


def test_name_trigger_does_not_edit_the_preserved_user_message(bot):
    async def run():
        bot.ai_state.set_settings(1, wake_enabled=True)

        async def addressed(*args):
            return True

        bot.assistant.addressed = addressed
        await receive(bot, "Луна, помоги", 5001)
        await bot.process(bot.store.claim_note())
        request = bot.ai_state.claim()
        assert request["trigger"] == "name"
        assert bot.telegram.sent == []
        bot.ai_state.update_request(1, request["note_id"], state="ready", response="Ответ ИИ")
        await bot._deliver_ai(request)
        assert bot.telegram.sent[-1][1]["reply_to"] == 5001
    asyncio.run(run())
