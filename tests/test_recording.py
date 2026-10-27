import asyncio

from support.base import make_bot, receive_voice
from support.commands import receive

from voice_memory_bot.config import Limits
from voice_memory_bot.store import Store


def test_folder_selection_survives_restart_and_pending_audio_keeps_its_destination(tmp_path, store):
    async def run():
        first = store.create_folder(1, "Работа", 100)
        second = store.create_folder(1, "Личное", 200)
        bot = make_bot(tmp_path, store)
        await bot.callback(1, None, 500, f"folder:{first['id']}:0")
        await receive_voice(bot, message_id=10, thread=200)
        await bot.callback(1, None, 500, f"folder:{second['id']}:0")
        store.close()
        restarted = Store(tmp_path / "archive.sqlite3", Limits())
        try:
            bot = make_bot(tmp_path, restarted)
            await bot.process(restarted.claim_note())
            await receive_voice(bot, message_id=20, thread=None)
            await bot.process(restarted.claim_note())
            assert restarted.one("SELECT folder_id FROM notes WHERE message_id=10")["folder_id"] == first["id"]
            assert restarted.one("SELECT folder_id FROM notes WHERE message_id=20")["folder_id"] == second["id"]
            assert restarted.current_folder(2) == restarted.ensure_user(2)
            assert restarted.folder(1, first["id"])["thread_id"] == 100
            assert not any(m in {"createForumTopic", "sendVoice"} for m, _ in bot.telegram.sent)
        finally:
            restarted.close()
    asyncio.run(run())


def test_bottom_buttons_create_rename_search_and_cancel_without_archiving_ui_input(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        await receive(bot, "/start")
        assert next(p for m, p in reversed(bot.telegram.sent) if m == "sendMessage")["keyboard"]["is_persistent"]
        await receive(bot, "＋ Новая папка", 2)
        await receive(bot, "Работа", 3)
        folder = next(f for f in store.folders(1) if f["name"] == "Работа")
        assert store.current_folder(1) == folder["id"]
        await bot.callback(1, None, 500, f"rename:{folder['id']}")
        await receive(bot, "Проекты", 4)
        assert store.folder(1, folder["id"])["name"] == "Проекты"
        await receive(bot, "🔎 Поиск", 5)
        await receive(bot, "＋ Новая папка", 6)
        await receive_voice(bot, 7, None)
        assert store.notes(1) == [] and store.action(1) == "create"
        await bot.callback(1, None, 500, "home")
        await receive(bot, "Идея", 8)
        await bot.process(store.claim_note())
        assert [n["transcript"] for n in store.notes(1)] == ["Идея"]
        await receive(bot, "🔎 Поиск", 9)
        await receive(bot, "идея", 10)
        assert store.notes(1)[0]["id"] in str(bot.bottom_state(1)["actions"])
        assert store.action(1) is None
        assert not any(m in {"createForumTopic", "editForumTopic"} for m, _ in bot.telegram.sent)
        for data in ("record:999", f"recordlink:999:{folder['id']}"):
            await bot.callback(1, None, 500, data)
        assert len(store.folders(1)) == 3
    asyncio.run(run())
