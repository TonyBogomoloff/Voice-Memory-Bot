import asyncio
import json
import re
from dataclasses import replace

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from support.commands import bot as bot
from support.commands import receive

from voice_memory_bot.obsidian_api import ExportAPI
from voice_memory_bot.providers import Telegram


@pytest.mark.parametrize('entry', ['settings_button', 'settings_command', 'folder'])
def test_visible_telegram_controls_deliver_usable_pairing_code(bot, entry):
    bot.config = replace(bot.config, obsidian_url='https://audio-notes.korobkoff.com')
    folder = bot.store.current_folder(1)

    async def run():
        sent = []

        def handler(request):
            payload = json.loads(request.content)
            sent.append((request.url.path.rsplit('/', 1)[-1], payload))
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1000 + len(sent)}})

        telegram = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        telegram.on_message = bot.telegram.on_message
        telegram.on_keyboard = bot.emoji_keyboards.remember
        bot.telegram = telegram

        def message():
            return next(payload for method, payload in reversed(sent) if method == 'sendMessage')

        async def tap(text):
            # Tap exactly the label sent over the real Telegram transport, including themed aliases.
            label = next(item['text'] for row in message()['reply_markup']['keyboard'] for item in row
                         if text in item['text'])
            await receive(bot, label, 2000 + len(sent))

        try:
            await bot.show_home(1)
            if entry == 'settings_command':
                await receive(bot, '/settings', 2000)
            else:
                await tap('Папка' if entry == 'folder' else 'Настройки')
            await tap('Obsidian')
            await tap('Подключить устройство')
            payload = message()
            assert bot.config.obsidian_url in payload['text']
            assert '10 минут' in payload['text']
            match = re.search(r'^([A-Za-z0-9_-]{24})$', payload['text'], re.MULTILINE)
            assert match, 'The delivered message must contain the pairing code, not just the heading'
            scope = folder if entry == 'folder' else 'all'
            async with TestClient(TestServer(ExportAPI(bot).application())) as client:
                response = await client.post('/v1/pair', json={'code': match[1], 'name': 'iPhone'})
                paired = await response.json()
                assert response.status == 200 and paired['scope'] == scope
            await tap('Назад')
            assert bot.bottom_state(1)['screen'] == (f'foldermenu:{folder}' if entry == 'folder' else 'settings:all')
            assert bot.store.current_folder(1) == folder
            assert bot.store.notes(1) == []
        finally:
            await telegram.close()

    asyncio.run(run())


def test_audio_defaults_folder_override_and_export_all_button(bot):
    from support.menus import labels

    from voice_memory_bot.obsidian_settings import ObsidianSettings

    async def run():
        folder = bot.store.current_folder(1)
        other = bot.store.create_folder(1, 'Другая')['id']
        preferences = ObsidianSettings(bot.store)
        await receive(bot, '/settings', 3000)
        await receive(bot, '💎 Obsidian', 3001)
        assert '📤 Экспортировать все папки' in labels(bot)
        assert preferences.audio(1, folder)
        await receive(bot, '🎙 Экспортировать аудио: вкл', 3002)
        assert not preferences.audio(1, folder) and not preferences.audio(1, other)
        await bot.obsidian_menu(1, None, folder)
        await receive(bot, '🎙 Экспортировать аудио: выкл', 3003)
        assert preferences.audio(1, folder) and not preferences.audio(1, other)
        assert '📤 Экспортировать папку' in labels(bot)
        await receive(bot, '↩️ Как в общих настройках', 3004)
        assert not preferences.audio(1, folder)
        foreign = bot.store.create_folder(2, 'Чужая')['id']
        with pytest.raises(ValueError):
            preferences.set_audio(1, foreign, False)
        assert preferences.audio(2, foreign)
        await bot.obsidian_menu(1, None)
        sent = []
        async def document(owner, path, thread=None, caption=None):
            sent.append(path.read_bytes())
        bot.telegram.document = document
        await receive(bot, '📤 Экспортировать все папки', 3005)
        await asyncio.gather(*bot.export_tasks)
        assert sent and sent[0].startswith(b'PK')

    asyncio.run(run())
