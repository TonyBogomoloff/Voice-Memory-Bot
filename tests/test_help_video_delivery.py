import asyncio
import json
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest

from voice_memory_bot.custom_emoji import IDS
from voice_memory_bot.providers import ProviderError, Telegram, silent_delivery


def multipart(request):
    message = BytesParser(policy=default).parsebytes(
        f'Content-Type: {request.headers["content-type"]}\r\n\r\n'.encode() + request.content)
    return {part.get_param('name', header='content-disposition'): part.get_payload(decode=True)
            for part in message.iter_parts()}


@pytest.mark.parametrize('inline', [False, True])
@pytest.mark.parametrize('outcome', ['success', 'emoji_rejection', 'timeout'])
def test_help_video_caption_keyboard_receipt_and_safe_retry(tmp_path, outcome, inline):
    async def run():
        path = tmp_path / 'demo.mp4'
        path.write_bytes(b'unchanged video')
        sent, receipts, aliases = [], [], []
        def handler(request):
            assert bool(aliases) is (not inline), 'Only reply keyboards update durable label aliases'
            assert request.url.path.endswith('/sendVideo')
            sent.append(multipart(request))
            if outcome == 'timeout':
                raise httpx.ReadTimeout('lost response')
            if outcome == 'emoji_rejection' and len(sent) == 1:
                return httpx.Response(400, json={'ok': False, 'error_code': 400,
                                                'description': 'Bad Request: CUSTOM_EMOJI_ID_INVALID'})
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 71}})
        telegram = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        telegram.on_message = lambda *args: receipts.append(args)
        telegram.on_keyboard = lambda *args: aliases.append(args)
        keys = ({'inline_keyboard': [[{'text': '⬅️ Назад', 'callback_data': 'help'}]]} if inline
                else {'keyboard': [[{'text': '📁 Папки и порядок'}]], 'is_persistent': True})
        token = silent_delivery.set(True)
        try:
            if outcome == 'timeout':
                with pytest.raises(ProviderError, match='telegram_transport_unknown'):
                    await telegram.video(1, path, 9, caption='📁 Папки', keyboard=keys)
            else:
                await telegram.video(1, path, 9, caption='📁 Папки', keyboard=keys)
        finally:
            silent_delivery.reset(token)
            await telegram.close()
        assert len(sent) == (2 if outcome == 'emoji_rejection' else 1)
        assert receipts == ([] if outcome == 'timeout' else [(1, 71, 9)])
        assert aliases == ([] if inline else [(1, {'Папки и порядок': '📁 Папки и порядок'})])
        if inline:
            assert json.loads(sent[0]['reply_markup']) == {'inline_keyboard': [[{
                'text': 'Назад', 'callback_data': 'help', 'icon_custom_emoji_id': IDS['back']}]]}
        for payload in sent:
            assert payload['video'] == b'unchanged video'
            assert payload['caption'].decode() == '📁 Папки'
            assert payload['show_caption_above_media'] == payload['supports_streaming'] == b'true'
            assert payload['disable_notification'] == b'true'
            assert payload['message_thread_id'] == b'9'
            assert (payload['duration'], payload['width'], payload['height']) == (b'10', b'720', b'1280')
        if outcome == 'emoji_rejection':
            assert 'caption_entities' in sent[0] and 'caption_entities' not in sent[1]
            assert json.loads(sent[1]['reply_markup']) == keys
    asyncio.run(run())
