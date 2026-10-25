import asyncio

from support.base import FakeGroq, FakeRouter, FakeTelegram
from support.commands import bot as bot
from support.commands import receive
from support.menus import labels, reply_keyboard

from voice_memory_bot.bot import Bot
from voice_memory_bot.bottom_menu import BACK, NEXT, PREVIOUS, ROOT


def last_message(bot):
    return next(p for m, p in reversed(bot.telegram.sent) if m == "sendMessage")


def test_keyboard_carrier_stays_until_next_keyboard_is_delivered(bot):
    async def run():
        await receive(bot, "/start", 1000)
        first = bot.store.one("SELECT message_id FROM bottom_menus WHERE owner=1")["message_id"]
        assert not any(m == "deleteMessage" and p["message_id"] == first for m, p in bot.telegram.sent)
        assert "\n" not in last_message(bot)["text"]
        bot.telegram.sent.clear()
        await receive(bot, "☰ Меню", 1002)
        current = bot.store.one("SELECT message_id FROM bottom_menus WHERE owner=1")["message_id"]
        assert current != first
        sent = next(i for i, (m, _) in enumerate(bot.telegram.sent) if m == "sendMessage")
        deleted = next(i for i, (m, p) in enumerate(bot.telegram.sent)
                       if m == "deleteMessage" and p["message_id"] == first)
        assert sent < deleted
        assert not any(m == "deleteMessage" and p["message_id"] == current for m, p in bot.telegram.sent)
    asyncio.run(run())


def test_menu_settings_back_cancel_and_plain_input(bot):
    async def run():
        folder = bot.store.current_folder(1)
        await receive(bot, "/start", 1000)
        assert reply_keyboard(bot)["input_field_placeholder"] == "📁 Входящие"
        await receive(bot, "☰ Меню", 1001)
        assert reply_keyboard(bot)["input_field_placeholder"] == "📁 Входящие"
        assert {"Включить кнопки под сообщениями", "ℹ️ Help", "🤖 Поговорить с ИИ", BACK, ROOT} == set(labels(bot))
        await receive(bot, "/settings", 1002)
        export = next(label for label in labels(bot) if label.startswith("📦 Экспорт"))
        await receive(bot, export, 1003)
        await receive(bot, "Markdown", 1004)
        assert bot.store.export_format(1) == "markdown" and "✓ Markdown" in labels(bot)
        await receive(bot, BACK, 1005)
        assert "📦 Экспорт · Markdown" in labels(bot)
        await receive(bot, "🤖 Настройки ИИ", 1006)
        await receive(bot, "Обращение по имени: выкл", 1007)
        assert bot.ai_state.settings(1)["wake_enabled"]
        await receive(bot, "Изменить имена", 1008)
        assert bot.store.action(1) == "ai_names"
        await receive(bot, BACK, 1009)
        assert bot.store.action(1) is None and bot.bottom_state(1)["screen"] == "aisettings"
        await receive(bot, ROOT, 1010)
        assert "☰ Меню" in labels(bot) and bot.store.current_folder(1) == folder
        assert bot.store.notes(1) == [] and bot.store.all("SELECT * FROM ai_requests") == []
        # Every navigation send uses a reply keyboard, no buttons attached to chat messages.
        assert all("inline_keyboard" not in (p.get("keyboard") or {})
                   for m, p in bot.telegram.sent if m == "sendMessage")
        sent_before = len(bot.telegram.sent)
        await receive(bot, "Это обычная заметка", 1011)
        note = bot.store.notes(1)[0]
        assert note["folder_id"] == folder and note["transcript"] == "Это обычная заметка"
        assert len(bot.telegram.sent) == sent_before and note["transcript_message_id"] == 1011
    asyncio.run(run())


def test_all_folders_are_reachable_and_stale_labels_never_archive(bot):
    async def run():
        for number in range(10):
            bot.store.create_folder(1, f"Папка {number}")
        await receive(bot, "/folders", 1000)
        seen = set()
        for page in range(3):
            state = bot.bottom_state(1)
            seen.update(action.split(":")[1] for action in state["actions"].values()
                        if action.startswith("folder:"))
            assert len(reply_keyboard(bot)["keyboard"]) <= 7
            if page < 2:
                await receive(bot, "Папки ➡️", 1001 + page)
        assert seen == {f["id"] for f in bot.store.folders(1)}
        stale = next(label for label, action in bot.bottom_state(1)["actions"].items()
                     if action.startswith("folder:"))
        await receive(bot, stale, 1004)
        selected = bot.store.current_folder(1)
        await receive(bot, stale, 1005)
        assert bot.store.current_folder(1) == selected and bot.store.notes(1) == []
        assert "устарела" in last_message(bot)["text"]
        assert reply_keyboard(bot)["input_field_placeholder"] == "📁 " + bot.store.folder(1, selected)["name"]
        await receive(bot, ROOT, 1006)
        await receive(bot, "☰ Меню", 1007)
        await receive(bot, "📁 Папка", 1008)
        await receive(bot, "✏️ Переименовать", 1009)
        await receive(bot, BACK, 1010)
        assert bot.store.action(1) is None
        assert bot.bottom_state(1)["screen"] == f"foldermenu:{selected}"
    asyncio.run(run())


def test_menu_binding_survives_restart_and_is_owner_scoped(bot):
    async def run():
        await receive(bot, "/settings", 1000)
        restarted = Bot(bot.config, bot.store, FakeTelegram(), FakeGroq(), FakeRouter())
        await receive(restarted, "🤖 Настройки ИИ", 1001)
        assert restarted.bottom_state(1)["screen"] == "aisettings"
        assert restarted.store.notes(1) == []
        restarted.store.ensure_user(2)
        assert not restarted.is_bottom_control(2, "🤖 Настройки ИИ")
        await restarted.handle({"message": {"message_id": 1002, "from": {"id": 2},
            "chat": {"id": 1, "type": "private"}, "text": "Обращение по имени: выкл"}})
        assert not restarted.ai_state.settings(1)["wake_enabled"]
    asyncio.run(run())


def test_confirmations_cancel_expire_and_do_not_delete_after_restart(bot):
    async def run():
        folder = bot.store.current_folder(1)
        await bot.callback(1, None, None, f"dropfolder:{folder}")
        assert "🗑 Да, удалить папку" in labels(bot)
        await receive(bot, BACK, 1001)
        await receive(bot, "🗑 Да, удалить папку", 1002)
        assert bot.store.folder(1, folder)["removed"] == 0
        await receive(bot, "/delete_all", 1003)
        restarted = Bot(bot.config, bot.store, FakeTelegram(), FakeGroq(), FakeRouter())
        await receive(restarted, "🗑 Да, удалить всё", 1004)
        assert restarted.store.folder(1, folder)["removed"] == 0
        assert "устарело" in last_message(restarted)["text"]
        await receive(restarted, "/delete_all", 1005)
        await receive(restarted, "🗑 Да, удалить всё", 1006)
        assert restarted.store.folders(1) == []
        assert restarted.store.all("SELECT * FROM bottom_menu_labels WHERE label LIKE '%Входящие%'") == []
        await receive(restarted, "🗑 Да, удалить всё", 1007)
        assert restarted.store.notes(1) == []
    asyncio.run(run())


def test_duplicate_search_titles_resolve_exact_notes_and_paginate(bot):
    async def run():
        folder = bot.store.current_folder(1)
        ids = set()
        for number in range(12):
            note, _ = bot.store.add_note(1, folder, {"message_id": 2000 + number}, "text", transcript=f"тест {number}")
            bot.store.update_note(1, note["id"], title="Повтор", state="ready", delivery="sent",
                                  transcript_delivery="sent")
            ids.add(note["id"])
        await receive(bot, "/search тест", 3000)
        seen = set()
        for page in range(3):
            state = bot.bottom_state(1)
            actions = state["actions"]
            seen.update(action.split(":")[1] for action in actions.values() if action.startswith("note:"))
            assert len(labels(bot)) == len(set(labels(bot)))
            if page < 2:
                await receive(bot, NEXT, 3001 + page)
        assert seen == ids
        await receive(bot, PREVIOUS, 3004)
        label, action = next((label, action) for label, action in bot.bottom_state(1)["actions"].items()
                             if action.startswith("note:"))
        expected = bot.store.note(1, action.split(":")[1])["transcript"]
        await receive(bot, label, 3005)
        assert last_message(bot)["text"] == expected
        assert last_message(bot)["keyboard"]["inline_keyboard"] == []
        assert len(bot.store.notes(1, limit=50)) == 12
    asyncio.run(run())


def test_note_options_remain_attached_and_do_not_change_bottom_screen(bot):
    async def run():
        bot.store.set_message_buttons(1, True)
        await receive(bot, "/settings", 1000)
        await receive(bot, "Запись", 1001)
        note = bot.store.notes(1)[0]
        await bot.callback(1, None, note["transcript_message_id"], f"options:{note['id']}")
        method, payload = bot.telegram.sent[-1]
        assert method == "editMessageReplyMarkup"
        actions = {b["callback_data"] for row in payload["reply_markup"]["inline_keyboard"] for b in row}
        assert {f"pick:{note['id']}:0", f"delete:{note['id']}", f"aiask:{note['id']}"} <= actions
        assert "folders" not in actions and bot.bottom_state(1)["screen"] == "settings:all"
        await bot.callback(1, None, note["transcript_message_id"], f"collapse:{note['id']}")
        assert bot.telegram.sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Переместить"
    asyncio.run(run())


def test_initial_folder_prompt_uses_bottom_controls(bot):
    async def run():
        await receive(bot, "/menu", 1000)
        assert not any("web_app" in b for row in reply_keyboard(bot)["keyboard"] for b in row)
        bot.store.delete_folder(1, bot.store.current_folder(1))
        await receive(bot, "/start", 1001)
        assert bot.store.action(1) == "create" and labels(bot) == ["Отмена"]
        await receive(bot, "Отмена", 1002)
        assert bot.store.action(1) is None and "🤖 Поговорить с ИИ" in labels(bot)
        await receive(bot, ROOT, 1003)
        assert bot.store.action(1) == "create"
    asyncio.run(run())


def test_selected_note_ai_keeps_bottom_menu_and_inline_cancel_survives_restart(bot):
    async def run():
        await receive(bot, "/start", 999)
        await receive(bot, "Исходник", 1000)
        note = bot.store.notes(1)[0]
        bottom, keyboard = bot.bottom_state(1), reply_keyboard(bot)
        await bot.callback(1, None, note["transcript_message_id"], f"aiask:{note['id']}")
        prompt_id = bot.telegram.message_sequence
        cancel = last_message(bot)['keyboard']['inline_keyboard'][0][0]
        assert cancel['text'] == 'Отмена'
        assert bot.bottom_state(1) == bottom and reply_keyboard(bot) == keyboard
        assert bot.store.action(1).startswith("ai_note:")
        assert bot.ai_state.claim() is None
        restarted = Bot(bot.config, bot.store, FakeTelegram(), FakeGroq(), FakeRouter())
        await restarted.callback(1, None, prompt_id, cancel['callback_data'])
        assert restarted.store.action(1) is None
        assert restarted.bottom_state(1) == bottom
        await receive(restarted, "Обычная заметка", 1002)
        assert restarted.ai_state.claim() is None
        assert restarted.store.note(1, note["id"])["transcript"] == "Исходник"
    asyncio.run(run())


def test_delete_all_buttons_are_red(bot):
    async def run():
        await bot.callback(1, None, None, "settings:all")
        controls = [b for row in reply_keyboard(bot)["keyboard"] for b in row if b["text"] == "🗑 Удалить всё"]
        assert controls and controls[0]["style"] == "danger"
        await bot.delete_all_menu(1)
        controls = [b for row in reply_keyboard(bot)["keyboard"] for b in row if b["text"] == "🗑 Да, удалить всё"]
        assert controls and controls[0]["style"] == "danger"
    asyncio.run(run())


def test_home_add_folder_and_options_back(bot):
    async def run():
        await receive(bot, "/start", 2000)
        assert reply_keyboard(bot)["keyboard"][-1][0]["text"] == "＋"
        await receive(bot, "＋", 2001)
        assert bot.store.action(1) == "create"
        await receive(bot, BACK, 2002)
        assert bot.bottom_state(1)["screen"] == "root"
        await receive(bot, "＋", 2003)
        await receive(bot, "Новая с главной", 2004)
        folder = bot.store.current_folder(1)
        assert bot.store.folder(1, folder)["name"] == "Новая с главной"
        await receive(bot, "📁 Папка", 2005)
        assert set(labels(bot)) == {"✏️ Переименовать", "🗑 Удалить папку", "📦 Экспорт", BACK}
        assert bot.bottom_state(1)["actions"]["📦 Экспорт"] == f"zip:{folder}:all"
        assert [[button["text"] for button in row] for row in reply_keyboard(bot)["keyboard"]] == [
            ["✏️ Переименовать", "🗑 Удалить папку"], [BACK, "📦 Экспорт"]]
        await receive(bot, BACK, 2006)
        assert bot.bottom_state(1)["screen"] == "root"
        assert bot.options_folder(1) is None
        assert bot.store.current_folder(1) == folder
        assert bot.store.notes(1) == []
    asyncio.run(run())


def test_minimal_menu_toggle_help_and_ai(bot):
    async def run():
        await receive(bot, "☰ Меню", 3000)
        assert not bot.store.message_buttons_enabled(1)
        await receive(bot, "Включить кнопки под сообщениями", 3001)
        assert bot.store.message_buttons_enabled(1)
        assert bot.bottom_state(1)["screen"] == "home"
        await receive(bot, "Выключить кнопки под сообщениями", 3002)
        assert not bot.store.message_buttons_enabled(1)
        await receive(bot, "ℹ️ Help", 3003)
        assert bot.bottom_state(1)["screen"] == "help"
        await receive(bot, BACK, 3004)
        await receive(bot, "🤖 Поговорить с ИИ", 3005)
        folder = bot.store.folder(1, bot.store.current_folder(1))
        assert folder["mode"] == "chat"
        assert bot.store.notes(1) == []
    asyncio.run(run())
