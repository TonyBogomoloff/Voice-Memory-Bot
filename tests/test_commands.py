import asyncio

import pytest
from support.commands import bot as bot
from support.commands import receive


@pytest.mark.parametrize("command, extra_folders", [
    ("/start", set()), ("/menu", set()), ("/help", set()), ("/privacy", set()), ("/folders", set()),
    ("/new", set()), ("/notes", set()), ("/search", set()), ("/export", set()),
    ("/app", set()), ("/settings", set()), ("/newchat", set()), ("/cancel", set()), ("/unknown", set()),
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


@pytest.mark.parametrize("command", ["/start", "/restart", "/menu", "/folders"])
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
        menu_calls = [c.kwargs for c in bot.telegram.call.call_args_list if c.args == ("setChatMenuButton",)]
        assert {"menu_button": {"type": "commands"}} in menu_calls
        assert {"chat_id": 1, "menu_button": {"type": "commands"}} in menu_calls
        commands = next(c.kwargs["commands"] for c in bot.telegram.call.call_args_list
                        if c.args == ("setMyCommands",))
        assert commands == [
            {"command": "ai", "description": "Изменить с ИИ"},
            {"command": "move", "description": "Перенести"},
            {"command": "delete", "description": "Удалить"},
            {"command": "options", "description": "Опции"},
            {"command": "restart", "description": "Перезапустить меню"},
        ]
    asyncio.run(run())


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("command", ["/restart", "/RESTART@your_audio_notes_bot payload"])
def test_restart_alias_restores_same_menu_as_start(bot, empty, command):
    from support.menus import reply_keyboard

    async def run():
        if empty:
            bot.store.delete_folder(1, bot.store.current_folder(1))
        else:
            await receive(bot, "Сохранённая запись", 100)
        notes = bot.store.notes(1)
        folder = bot.store.current_folder(1)
        await receive(bot, "/start", 200)
        expected_state, expected_keyboard = bot.bottom_state(1), reply_keyboard(bot)
        await receive(bot, "/new", 300)
        await receive(bot, command, 400)
        assert bot.bottom_state(1) == expected_state
        assert reply_keyboard(bot) == expected_keyboard
        assert bot.store.current_folder(1) == folder
        assert bot.store.notes(1) == notes
    asyncio.run(run())
