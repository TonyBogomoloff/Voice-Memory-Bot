import asyncio
import json
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest

from voice_memory_bot.providers import Telegram, silent_delivery


@pytest.mark.parametrize('method', ['sendMessage', 'sendVoice', 'sendVideoNote', 'sendMediaGroup',
                                   'copyMessage', 'copyMessages', 'forwardMessage', 'forwardMessages',
                                   'pinChatMessage'])
@pytest.mark.parametrize('notification', [None, False])
def test_message_requests_force_silent_delivery(method, notification):
    async def run():
        sent = []
        def handler(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 11}})
        telegram = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            await telegram.call(method, chat_id=1, disable_notification=notification)
        finally:
            await telegram.close()
        assert sent == [{'chat_id': 1, 'disable_notification': True}]
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['text', 'document', 'photo', 'video', 'help_photo', 'help_video'])
def test_all_delivery_helpers_and_emoji_retries_remain_silent(tmp_path, kind):
    async def run():
        suffix = '.mp4' if kind in {'video', 'help_video'} else '.png'
        path = tmp_path / ('attachment' + suffix)
        path.write_bytes(b'attachment')
        sent = []
        def handler(request):
            if request.headers['content-type'].startswith('multipart/'):
                message = BytesParser(policy=default).parsebytes(
                    f'Content-Type: {request.headers["content-type"]}\r\n\r\n'.encode() + request.content)
                payload = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True)
                           for part in message.iter_parts()}
                assert payload['disable_notification'] == b'true'
            else:
                payload = json.loads(request.content)
                assert payload['disable_notification'] is True
            sent.append(payload)
            if len(sent) == 1:
                return httpx.Response(400, json={'ok': False, 'error_code': 400,
                                                'description': 'CUSTOM_EMOJI_ID_INVALID'})
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 11}})
        telegram = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        # An older caller's context cannot turn notifications back on.
        token = silent_delivery.set(False)
        try:
            if kind == 'text':
                await telegram.text(1, '📁 Архив')
            elif kind.startswith('help_'):
                await telegram.help_media(1, path, caption='📁 Архив')
            else:
                await getattr(telegram, kind)(1, path, caption='📁 Архив')
        finally:
            silent_delivery.reset(token)
            await telegram.close()
        assert len(sent) == 2
        assert 'entities' not in sent[-1] and 'caption_entities' not in sent[-1]
    asyncio.run(run())


@pytest.mark.parametrize('method', ['getUpdates', 'editMessageText', 'editMessageMedia',
                                   'answerCallbackQuery', 'sendChatAction'])
def test_control_requests_do_not_receive_notification_parameter(method):
    async def run():
        sent = []
        def handler(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={'ok': True, 'result': True})
        telegram = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            await telegram.call(method)
        finally:
            await telegram.close()
        assert sent == [{}]
    asyncio.run(run())
