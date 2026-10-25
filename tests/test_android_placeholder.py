import asyncio

from support.commands import bot as bot
from support.commands import receive
from support.folders import deleted_ids

from voice_memory_bot.custom_emoji import themed_markup


def test_android_open_keyboard_hint_changes_with_folder_and_rename(bot):
    async def run():
        first = bot.store.current_folder(1)
        bot.store.rename_folder(1, first, 'Test 1')
        bot.store.create_folder(1, 'Test 2')
        original = bot.telegram.text
        opened, hint = False, None
        resets = []

        async def android(owner, text, thread=None, keyboard=None, **kwargs):
            nonlocal opened, hint
            result = await original(owner, text, thread, keyboard, **kwargs)
            markup, _ = themed_markup(keyboard or {})
            # Telegram Android refreshes the hint on opening the keyboard, but
            # setButtons() alone does not refresh it while the panel is open.
            if markup.get('remove_keyboard'):
                opened = False
                resets.append(result['message_id'])
            elif markup.get('keyboard'):
                if not opened:
                    hint = markup['input_field_placeholder']
                opened = True
            return result

        bot.telegram.text = android
        await receive(bot, '📁 Test 1', 1001)
        assert hint == 'Test 1' and not resets
        for mid, name in [(1002, 'Test 2'), (1003, 'Test 1')]:
            await receive(bot, '📁 ' + name, mid)
            assert hint == name
        assert len(resets) == 2
        await bot.show_main_menu(1)
        assert hint == 'Test 1' and len(resets) == 2
        bot.store.rename_folder(1, first, 'Renamed')
        await bot.show_home(1)
        assert hint == 'Renamed' and len(resets) == 3
        # Unknown client state after a restart is repaired on the next menu send.
        bot.init_bottom_menu()
        await bot.show_home(1)
        assert hint == 'Renamed' and len(resets) == 4
        assert set(resets) <= deleted_ids(bot)
        carrier = bot.store.one('SELECT message_id FROM bottom_menus WHERE owner=1')['message_id']
        assert carrier not in deleted_ids(bot)
        assert bot.store.notes(1) == []
    asyncio.run(run())
