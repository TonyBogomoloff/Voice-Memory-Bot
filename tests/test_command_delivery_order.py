import asyncio

import pytest
from support.commands import bot as bot
from support.commands import receive
from support.menus import reply_keyboard

from voice_memory_bot.bottom_menu import BACK
from voice_memory_bot.providers import ProviderError


@pytest.mark.parametrize('command', ['/new', '＋ Новая папка', '/settings', '/help', '/options', '/move', '/delete', 'Отмена'])
def test_response_precedes_command_cleanup(bot, command):
    async def run():
        bot.telegram.message_sequence = 1000
        await receive(bot, 'Заметка', 100)
        await receive(bot, '/new' if command == 'Отмена' else '/start', 101)
        bot.telegram.sent.clear()
        await receive(bot, command, 200)
        sent = next(i for i, (m, _) in enumerate(bot.telegram.sent) if m == 'sendMessage')
        removed = next(i for i, (m, p) in enumerate(bot.telegram.sent)
                       if m == 'deleteMessage' and p['message_id'] == 200)
        assert sent < removed
    asyncio.run(run())


def test_failed_response_preserves_user_command(bot):
    async def run():
        async def fail(*args, **kwargs):
            raise ProviderError('telegram_503')
        bot.telegram.text = fail
        with pytest.raises(ProviderError):
            await receive(bot, '/new', 200)
        assert not any(m == 'deleteMessage' and p['message_id'] == 200 for m, p in bot.telegram.sent)
        assert not bot.command_cleanups
    asyncio.run(run())


def test_settings_back_is_next_to_export(bot):
    async def run():
        await receive(bot, '/settings', 200)
        rows = reply_keyboard(bot)['keyboard']
        assert rows[1][0]['text'].startswith('📦 Экспорт')
        assert rows[1][1]['text'] == BACK
        assert sum(b['text'] == BACK for row in rows for b in row) == 1
    asyncio.run(run())


def test_background_export_keeps_command_until_response(bot):
    async def run():
        bot.telegram.message_sequence = 1000
        ready = asyncio.Event()

        async def export(*args):
            await ready.wait()
            await bot._show(1, 'Готовлю ZIP…')

        bot.send_export = export
        await bot.show_bottom(1, 'Экспорт', keys={'inline_keyboard': [[
            {'text': 'Скачать архив', 'callback_data': 'zip:all:raw'}]]}, screen='export:all')
        bot.telegram.sent.clear()
        await receive(bot, 'Скачать архив', 200)
        assert not any(m == 'deleteMessage' and p['message_id'] == 200 for m, p in bot.telegram.sent)
        ready.set()
        await asyncio.gather(*bot.export_tasks)
        assert [m for m, _ in bot.telegram.sent] == ['sendMessage', 'deleteMessage']
        assert bot.telegram.sent[-1][1]['message_id'] == 200
    asyncio.run(run())


@pytest.mark.parametrize('command', ['/move', '/ai', '/delete', '/ai@your_audio_notes_bot', '/Options', '/options@your_audio_notes_bot'])
@pytest.mark.parametrize('target', [100, 999])
def test_reply_action_command_stays_visible(bot, command, target):
    async def run():
        bot.telegram.message_sequence = 1000
        await receive(bot, 'Заметка', 100)
        bot.telegram.sent.clear()
        await bot.handle({'message': {'message_id': 200, 'from': {'id': 1},
            'chat': {'id': 1, 'type': 'private'}, 'text': command,
            'reply_to_message': {'message_id': target}}})
        assert any(m == 'sendMessage' for m, _ in bot.telegram.sent)
        assert not any(m == 'deleteMessage' and p['message_id'] == 200 for m, p in bot.telegram.sent)
        assert not bot.command_cleanups
    asyncio.run(run())
