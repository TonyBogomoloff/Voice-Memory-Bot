import asyncio
from email.parser import BytesParser
from email.policy import default
from unittest.mock import AsyncMock

import httpx
import pytest
from support.commands import bot as bot
from support.commands import receive
from support.menus import reply_keyboard

from voice_memory_bot.bot import START_CAPTION, START_VIDEO
from voice_memory_bot.providers import ProviderError, Telegram


@pytest.mark.parametrize('new_user', [False, True])
def test_start_shows_intro_each_time_then_usable_menu_without_changing_notes(bot, new_user):
    async def run():
        if new_user:
            bot.store.delete_folder(1, bot.store.current_folder(1))
        else:
            idea = bot.store.create_folder(1, 'Идеи')
            bot.store.select(1, idea['id'])
            await receive(bot, 'Идея для новой истории', 100)
        notes, folders = bot.store.notes(1), bot.store.folders(1)
        active = bot.store.current_folder(1)
        bot.telegram.sent.clear()
        for message_id, command in [(200, '/start'), (300, '/START@your_audio_notes_bot invitation')]:
            await receive(bot, command, message_id, thread=99)
            assert bot.bottom_state(1)['screen'] == ('create' if new_user else 'root')
            assert reply_keyboard(bot)['is_persistent']
            assert bot.store.current_folder(1) == active
            assert bot.store.notes(1) == notes
            assert bot.store.folders(1) == folders
        videos = [payload for method, payload in bot.telegram.sent if method == 'sendVideo']
        assert len(videos) == 2
        assert all(v['path'] == str(START_VIDEO) and v['caption'] == START_CAPTION
                   and v['duration'] == 30 and v['thread'] == 99 for v in videos)
        assert START_VIDEO.is_file()
        sends = [method for method, _ in bot.telegram.sent if method.startswith('send')]
        assert sends == ['sendVideo', 'sendMessage', 'sendVideo', 'sendMessage']
    asyncio.run(run())


@pytest.mark.parametrize('error', [ProviderError('telegram_transport_unknown'), FileNotFoundError()])
def test_start_video_failure_keeps_caption_and_navigation_without_retry(bot, error):
    async def run():
        bot.telegram.video = AsyncMock(side_effect=error)
        await receive(bot, '/start', 200)
        assert bot.telegram.video.await_count == 1
        assert any(p.get('text') == START_CAPTION for m, p in bot.telegram.sent if m == 'sendMessage')
        assert bot.bottom_state(1)['screen'] == 'root'
        assert reply_keyboard(bot)['is_persistent']
        assert bot.store.notes(1) == []
    asyncio.run(run())


def test_start_uploads_full_portrait_video_with_caption_above_media(bot):
    async def run():
        uploads = []
        def handler(request):
            if request.url.path.endswith('/sendVideo'):
                message = BytesParser(policy=default).parsebytes(
                    f'Content-Type: {request.headers["content-type"]}\r\n\r\n'.encode() + request.content)
                uploads.append({part.get_param('name', header='content-disposition'): part.get_payload(decode=True)
                                for part in message.iter_parts()})
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1000 + len(uploads)}})
        telegram = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        telegram.on_message = bot.telegram.on_message
        telegram.on_keyboard = bot.emoji_keyboards.remember
        bot.telegram = telegram
        try:
            await receive(bot, '/start', 200, thread=99)
        finally:
            await telegram.close()
        assert len(uploads) == 1
        payload = uploads[0]
        assert payload['video'] == START_VIDEO.read_bytes()
        assert payload['caption'].decode() == START_CAPTION
        assert payload['show_caption_above_media'] == payload['supports_streaming'] == b'true'
        assert (payload['duration'], payload['width'], payload['height']) == (b'30', b'720', b'1280')
        assert payload['message_thread_id'] == b'99'
        assert bot.store.notes(1) == []
    asyncio.run(run())
