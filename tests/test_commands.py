import asyncio

import pytest
from test_mvp import make_bot

from voice_memory_bot.config import Limits
from voice_memory_bot.store import Store


@pytest.fixture
def bot(tmp_path):
    store = Store(tmp_path / "archive.sqlite3", Limits())
    store.ensure_user(1)
    app = make_bot(tmp_path, store)
    yield app
    store.close()


async def receive(bot, text, message_id=1, thread=None):
    await bot.handle({"message": {"message_id": message_id, "from": {"id": 1},
                     "chat": {"id": 1, "type": "private"}, "message_thread_id": thread, "text": text}})


@pytest.mark.parametrize("command, extra_folders", [
    ("/start", set()), ("/menu", set()), ("/help", set()), ("/privacy", set()), ("/folders", set()),
    ("/new", set()), ("/notes", set()), ("/search", set()), ("/export", set()),
    ("/settings", set()), ("/newchat", set()), ("/cancel", set()), ("/unknown", set()),
    ("/START@your_audio_notes_bot payload", set()),
    ("/new Работа", {"Работа"}), ("/ai", set()),
])
def test_commands_do_not_import_unknown_threads_as_folders(bot, command, extra_folders):
    async def run():
        await receive(bot, command, thread=99)
        assert {f["name"] for f in bot.store.folders(1)} == {"Входящие"} | extra_folders
        assert bot.store.folder_for_thread(1, 99) is None
        assert bot.store.notes(1) == []
        assert bot.telegram.sent
    asyncio.run(run())


@pytest.mark.parametrize("command", ["/start", "/menu", "/folders"])
def test_navigation_commands_cancel_pending_folder_input(bot, command):

    async def run():
        await receive(bot, "/new")
        await receive(bot, command, message_id=2)
        await receive(bot, "Заметка после команды", message_id=3)
        assert {f["name"] for f in bot.store.folders(1)} == {"Входящие"}
        assert bot.store.notes(1)[0]["transcript"] == "Заметка после команды"
    asyncio.run(run())


def test_folder_name_input_does_not_import_unknown_source_thread(bot):
    async def run():
        await receive(bot, "/new")
        await receive(bot, "Работа", message_id=2, thread=99)
        assert {f["name"] for f in bot.store.folders(1)} == {"Входящие", "Работа"}
        assert bot.store.folder_for_thread(1, 99) is None
        assert bot.store.notes(1) == []
    asyncio.run(run())


def test_start_registers_menu_commands(bot):
    from unittest.mock import AsyncMock

    async def run():
        bot.telegram.call = AsyncMock(return_value={"id": 123})
        for name in ("poll", "dispatch", "worker", "ai_worker", "folder_view_worker"):
            setattr(bot, name, AsyncMock())
        await bot.start()
        commands = next(c.kwargs["commands"] for c in bot.telegram.call.call_args_list
                        if c.args == ("setMyCommands",))
        assert commands == [
            {"command": "ai", "description": "🤖 Изменить с ИИ"},
            {"command": "move", "description": "↪️ Перенести запись"},
            {"command": "delete", "description": "🗑 Удалить запись"},
            {"command": "options", "description": "🎛️ Опции"},
        ]
    asyncio.run(run())
