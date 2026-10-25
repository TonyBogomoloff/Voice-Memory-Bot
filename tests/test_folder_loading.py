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
        assert [b['text'] for b in styled] == ['✓ 📌 ' + bot.store.folder(1, selected)['name']]
