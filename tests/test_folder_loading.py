import asyncio

import pytest
from test_commands import bot as bot
from test_folder_view import saved

from voice_memory_bot.providers import ProviderError


@pytest.mark.parametrize('fail', [False, True])
def test_loading_lives_until_delivery_finishes(bot, fail):
    async def run():
        folder = bot.store.current_folder(1)
        saved(bot, folder, 'Последняя запись', 1001)
        original = bot.telegram.text
        loading_id = None

        async def text(owner, content, *args, **kwargs):
            nonlocal loading_id
            if content == 'Последняя запись':
                assert loading_id is not None
                assert not any(m == 'deleteMessage' and p['message_id'] == loading_id
                               for m, p in bot.telegram.sent)
                if fail:
                    raise ProviderError('telegram_transport_unknown')
            result = await original(owner, content, *args, **kwargs)
            if content == 'Загружаю папку…':
                loading_id = result['message_id']
            return result

        bot.telegram.text = text
        if fail:
            with pytest.raises(ProviderError):
                await bot.show_notes(1, None, folder)
        else:
            await bot.show_notes(1, None, folder)
        assert bot.telegram.sent[-1] == ('deleteMessage', {'chat_id': 1, 'message_id': loading_id})
        assert bot.store.one('SELECT dirty FROM folder_views WHERE owner=1')['dirty'] == int(fail)
        assert 1 not in bot.view_rendering
    asyncio.run(run())


def test_only_selected_folder_has_primary_style(bot):
    other = bot.store.create_folder(1, 'Работа')['id']
    for selected in [bot.store.current_folder(1), other]:
        bot.store.select(1, selected)
        buttons = [b for row in bot.folder_keyboard(1)['keyboard'] for b in row]
        styled = [b for b in buttons if b.get('style') == 'primary']
        assert [b['text'] for b in styled] == ['✅ ' + bot.store.folder(1, selected)['name']]


@pytest.mark.parametrize("fail", [False, True])
def test_create_progress_is_visible_during_cleanup_and_removed(bot, fail):
    async def run():
        original = bot.clear_folder_view
        progress_id = None

        async def clear(*args, **kwargs):
            nonlocal progress_id
            assert bot.telegram.sent[-1][1]["text"] == "Создаю папку…"
            progress_id = bot.telegram.message_sequence
            assert progress_id in kwargs["keep_ids"]
            if fail:
                raise ProviderError("telegram_transport_unknown")
            return await original(*args, **kwargs)

        bot.clear_folder_view = clear
        if fail:
            with pytest.raises(ProviderError):
                await bot.create_folder(1, None, "Новая папка")
        else:
            await bot.create_folder(1, None, "Новая папка")
        assert bot.telegram.sent[-1] == ("deleteMessage", {"chat_id": 1, "message_id": progress_id})
        assert any(f["name"] == "Новая папка" for f in bot.store.folders(1))
    asyncio.run(run())
