import asyncio

import pytest
from support.base import FakeGroq, FakeRouter, FakeTelegram
from support.commands import bot as bot
from support.commands import receive
from support.menus import reply_keyboard

from voice_memory_bot.bot import Bot
from voice_memory_bot.bottom_menu import BACK
from voice_memory_bot.custom_emoji import IDS, themed_markup
from voice_memory_bot.help_menu import HELP_INTRO, HELP_TOPICS, help_video_path
from voice_memory_bot.states import MenuState


def last_message(bot):
    return next(p for m, p in reversed(bot.telegram.sent) if m in {"sendMessage", "sendVideo", "sendPhoto", "editMessageMedia"})


async def tap(bot, message, data):
    return await bot.handle({"callback_query": {"id": "help-tap", "from": {"id": 1}, "data": data,
        "message": {"message_id": message["message_id"], "chat": {"id": 1, "type": "private"}}}})


def assert_index(bot):
    payload = last_message(bot)
    assert payload["caption"] == HELP_INTRO == ""
    rows = payload["keyboard"]["inline_keyboard"]
    assert [len(row) for row in rows] == [2, 2, 2, 2]
    assert [b["callback_data"] for row in rows for b in row] == [f"help:{key}" for key in HELP_TOPICS]
    themed, aliases = themed_markup(payload["keyboard"])
    assert not aliases
    assert [b["icon_custom_emoji_id"] for row in themed["inline_keyboard"] for b in row] == [
        IDS[key] for key in ("microphone", "folder", "ai", "photo", "search", "move", "export", "settings")]


@pytest.mark.parametrize("topic", HELP_TOPICS)
def test_inline_help_and_back_keep_folder_keyboard_and_notes(bot, topic):
    async def run():
        bot.telegram.message_sequence = 1000
        await receive(bot, "Моя запись", 100)
        await receive(bot, "/start", 101)
        folder, notes = bot.store.current_folder(1), bot.store.notes(1)
        keys = reply_keyboard(bot)
        carrier = bot.store.one("SELECT message_id FROM bottom_menus WHERE owner=1")["message_id"]
        index = await bot.command(1, None, "/help", "")
        assert_index(bot)
        demo = await tap(bot, index, f"help:{topic}")
        video = last_message(bot)
        assert video["caption"] == "\n\n".join(HELP_TOPICS[topic])
        assert video["path"] == str(help_video_path(topic)) and help_video_path(topic).is_file()
        assert len(video["caption"]) <= 1024
        assert video["keyboard"] == {"inline_keyboard": [[{"text": BACK, "callback_data": "help"}]]}
        themed, aliases = themed_markup(video["keyboard"])
        assert not aliases and themed["inline_keyboard"] == [[{
            "text": "Назад", "callback_data": "help", "icon_custom_emoji_id": IDS["back"]}]]
        await tap(bot, demo, "help")
        assert_index(bot)
        assert bot.bottom_state(1)["screen"] == "root" and reply_keyboard(bot) == keys
        deleted = [p["message_id"] for m, p in bot.telegram.sent if m == "deleteMessage"]
        assert index["message_id"] == demo["message_id"]
        assert index["message_id"] not in deleted and carrier not in deleted
        assert bot.store.current_folder(1) == folder and bot.store.notes(1) == notes
        assert bot.store.all("SELECT * FROM ai_requests") == []
        await receive(bot, "/options", 105)
        assert bot.bottom_state(1)["screen"] == f"recordoptions:{notes[0]['id']}"
    asyncio.run(run())


def test_inline_help_survives_restart_and_folder_buttons_still_work(bot):
    async def run():
        bot.telegram.message_sequence = 1000
        original = bot.store.current_folder(1)
        second = bot.store.create_folder(1, "Папки и порядок")
        with bot.store.transaction():
            bot.store.db.execute("INSERT INTO bottom_menu_labels(owner,label) VALUES (1,?)",
                                 (HELP_TOPICS["folders"][0],))
        bot.store.select(1, original)
        index = await bot.show_help(1)
        restarted = Bot(bot.config, bot.store, FakeTelegram(), FakeGroq(), FakeRouter())
        restarted.telegram.message_sequence = 2000
        demo = await tap(restarted, index, "help:ai")
        assert last_message(restarted)["caption"] == "\n\n".join(HELP_TOPICS["ai"])
        await tap(restarted, demo, "help")
        assert_index(restarted)
        await receive(restarted, "📁 Папки и порядок", 102)
        assert restarted.store.current_folder(1) == second["id"]
        assert restarted.bottom_state(1)["screen"] == "root"
        assert restarted.store.notes(1) == [] and restarted.store.all("SELECT * FROM ai_requests") == []
    asyncio.run(run())


def test_legacy_help_keyboard_and_delayed_labels_open_inline_help(bot):
    async def run():
        state = MenuState.from_mapping({"screen": "help", "back": "home", "text": HELP_INTRO,
            "rows": [], "help_topic": "save"})
        with bot.store.transaction():
            bot.store.db.execute("INSERT INTO bottom_menus(owner,state) VALUES (1,?)", (state.to_json(),))
            bot.store.db.executemany("INSERT INTO bottom_menu_labels(owner,label) VALUES (1,?)",
                [(title,) for title, _ in HELP_TOPICS.values()])
        await bot.send_bottom(1, None, state)
        assert bot.bottom_state(1)["screen"] == "root"
        await receive(bot, HELP_TOPICS["export"][0], 102)
        assert last_message(bot)["caption"] == "\n\n".join(HELP_TOPICS["export"])
        assert bot.bottom_state(1)["screen"] == "root" and bot.store.notes(1) == []
    asyncio.run(run())


def test_help_before_first_folder_and_unknown_topic_returns_to_index(bot):
    async def run():
        bot.store.delete_folder(1, bot.store.current_folder(1))
        await receive(bot, "/start", 100)
        assert bot.store.action(1) == "create"
        await receive(bot, "/HELP@your_audio_notes_bot", 101)
        assert bot.store.action(1) is None and bot.bottom_state(1)["screen"] == "root"
        assert_index(bot)
        demo = await bot.callback(1, None, None, "help:folders")
        await tap(bot, demo, "help:old-topic")
        assert_index(bot)
        assert [b["text"] for row in reply_keyboard(bot)["keyboard"] for b in row] == ["＋", "📁 Папка", "🎛️ Настройки"]
        await receive(bot, "＋", 103)
        assert bot.store.action(1) == "create"
        assert bot.store.folders(1) == [] and bot.store.notes(1) == []
    asyncio.run(run())


def test_help_video_failure_keeps_index_and_folder_carrier_without_retry(bot):
    from voice_memory_bot.providers import ProviderError

    async def run():
        index = await bot.show_help(1)
        carrier = bot.store.one("SELECT message_id FROM bottom_menus WHERE owner=1")["message_id"]
        bot.telegram.sent.clear()
        bot.telegram.fail_delivery = True
        with pytest.raises(ProviderError, match="telegram_transport_unknown"):
            await bot.callback(1, None, index["message_id"], "help:save")
        assert [method for method, _ in bot.telegram.sent] == ["editMessageMedia"]
        assert bot.store.one("SELECT message_id FROM bottom_menus WHERE owner=1")["message_id"] == carrier
        bot.telegram.fail_delivery = False
        await bot.callback(1, None, index["message_id"], "help:folders")
        assert [m for m, _ in bot.telegram.sent] == ["editMessageMedia", "editMessageMedia"]
        assert bot.telegram.sent[-1][1]["message_id"] == index["message_id"]
    asyncio.run(run())


def test_missing_help_video_keeps_explanation_and_inline_back(bot, monkeypatch, tmp_path):
    monkeypatch.setattr("voice_memory_bot.bot.help_video_path", lambda topic: tmp_path / "missing.mp4")
    asyncio.run(bot.show_help(1, topic="save"))
    assert last_message(bot)["caption"] == "\n\n".join(HELP_TOPICS["save"])
    assert last_message(bot)["keyboard"] == {"inline_keyboard": [[{"text": BACK, "callback_data": "help"}]]}
    assert bot.bottom_state(1)["screen"] == "root"
    assert not any(method == "sendVideo" for method, _ in bot.telegram.sent)
