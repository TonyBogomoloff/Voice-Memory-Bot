import asyncio

import pytest
from support.commands import bot as bot
from support.commands import receive
from support.folders import deleted_ids, saved


@pytest.mark.parametrize("busy", [False, True])
def test_folder_switch_replaces_keyboard_without_hiding_buttons(bot, busy):
    async def run():
        first = bot.store.current_folder(1)
        bot.store.rename_folder(1, first, "Test 1")
        bot.store.create_folder(1, "Test 2")
        await receive(bot, "📁 Test 1", 1001)
        if busy:
            note = saved(bot, first, "Обрабатывается", 900)
            bot.store.update_note(1, note["id"], state="queued", delivery="pending")
        for mid, name in [(1002, "Test 2"), (1003, "Test 1")]:
            bot.telegram.sent.clear()
            await receive(bot, "📁 " + name, mid)
            markups = [p.get("keyboard") or {} for m, p in bot.telegram.sent if m == "sendMessage"]
            assert not any(k.get("remove_keyboard") for k in markups)
            keyboard = next(k for k in reversed(markups) if k.get("keyboard"))
            assert keyboard["is_persistent"] is True
            assert keyboard["one_time_keyboard"] is False
            assert keyboard["input_field_placeholder"] == "📁 " + name
            assert any(b["text"] == "✅ " + name for row in keyboard["keyboard"] for b in row)
            carrier = bot.store.one("SELECT message_id FROM bottom_menus WHERE owner=1")["message_id"]
            assert carrier not in deleted_ids(bot)
        bot.store.rename_folder(1, first, "Renamed")
        bot.init_bottom_menu()
        bot.telegram.sent.clear()
        await bot.show_home(1)
        markups = [p.get("keyboard") or {} for m, p in bot.telegram.sent if m == "sendMessage"]
        assert not any(k.get("remove_keyboard") for k in markups)
        assert next(k for k in markups if k.get("keyboard"))["input_field_placeholder"] == "📁 Renamed"
    asyncio.run(run())
