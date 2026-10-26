import asyncio

import pytest
from support.base import FakeGroq, FakeRouter, FakeTelegram
from support.commands import bot as bot
from support.commands import receive
from support.menus import labels, reply_keyboard

from voice_memory_bot.bot import Bot
from voice_memory_bot.bottom_menu import BACK, ROOT
from voice_memory_bot.help_menu import HELP_TOPICS


def last_text(bot):
    return next(p["text"] for m, p in reversed(bot.telegram.sent) if m == "sendMessage")


@pytest.mark.parametrize("topic", HELP_TOPICS)
def test_help_topics_are_reachable_and_back_returns_to_index_without_changing_notes(bot, topic):
    async def run():
        bot.telegram.message_sequence = 1000
        await receive(bot, "Моя запись", 100)
        folder, notes = bot.store.current_folder(1), bot.store.notes(1)
        await receive(bot, "/help", 101)
        index = reply_keyboard(bot)["keyboard"]
        assert [len(row) for row in index] == [2, 2, 2, 2, 2]
        assert index[-1] == [{"text": BACK}, {"text": ROOT}]
        await receive(bot, HELP_TOPICS[topic][0], 102)
        assert bot.bottom_state(1)["screen"] == f"help:{topic}"
        assert last_text(bot) == "\n\n".join(HELP_TOPICS[topic])
        assert labels(bot) == [BACK, ROOT]
        await receive(bot, BACK, 103)
        assert bot.bottom_state(1)["screen"] == "help"
        assert reply_keyboard(bot)["keyboard"] == index
        await receive(bot, ROOT, 104)
        assert bot.bottom_state(1)["screen"] == "root"
        assert bot.store.current_folder(1) == folder and bot.store.notes(1) == notes
        assert bot.store.all("SELECT * FROM ai_requests") == []
        # Help messages must never replace the latest record targeted by Options.
        await receive(bot, "/options", 105)
        assert bot.bottom_state(1)["screen"] == f"recordoptions:{notes[0]['id']}"
    asyncio.run(run())


def test_help_topic_back_survives_restart_and_stale_topic_taps_are_not_notes(bot):
    async def run():
        await receive(bot, "/help", 100)
        await receive(bot, HELP_TOPICS["ai"][0], 101)
        restarted = Bot(bot.config, bot.store, FakeTelegram(), FakeGroq(), FakeRouter())
        await receive(restarted, BACK, 102)
        assert restarted.bottom_state(1)["screen"] == "help"
        await receive(restarted, ROOT, 103)
        await receive(restarted, HELP_TOPICS["ai"][0], 104)
        assert "устарела" in last_text(restarted)
        assert restarted.store.notes(1) == []
        assert restarted.store.all("SELECT * FROM ai_requests") == []
    asyncio.run(run())


def test_help_is_available_before_first_folder_and_unknown_topic_returns_to_index(bot):
    async def run():
        bot.store.delete_folder(1, bot.store.current_folder(1))
        await receive(bot, "/start", 100)
        assert bot.store.action(1) == "create"
        await receive(bot, "/HELP@your_audio_notes_bot", 101)
        assert bot.store.action(1) is None
        assert bot.bottom_state(1)["screen"] == "help"
        await receive(bot, HELP_TOPICS["folders"][0], 102)
        assert "«＋»" in last_text(bot)
        await bot.callback(1, None, None, "help:old-topic")
        assert bot.bottom_state(1)["screen"] == "help"
        await receive(bot, ROOT, 103)
        assert bot.store.action(1) == "create"
        assert bot.store.folders(1) == [] and bot.store.notes(1) == []
    asyncio.run(run())
