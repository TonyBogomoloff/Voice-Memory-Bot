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
