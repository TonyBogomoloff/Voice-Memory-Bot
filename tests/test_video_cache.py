import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from support.commands import bot as bot

from voice_memory_bot.bot import Bot
from voice_memory_bot.custom_emoji import IDS
from voice_memory_bot.help_menu import HELP_COVER, HELP_TOPICS, help_video_path
from voice_memory_bot.providers import ProviderError, Telegram, silent_delivery
from voice_memory_bot.video_cache import CACHE_NAME, VideoFileCache


def preparation_module():
    spec = importlib.util.spec_from_file_location('prepare_video_cache', Path(__file__).parents[1] / 'scripts/prepare_video_cache.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def telegram_response(result):
    return httpx.Response(200, json={'ok': True, 'result': result})


def test_preparation_survives_restart_and_first_help_request_has_no_upload(bot, monkeypatch):
    async def run():
        prepare = preparation_module()
        async def no_wait(_):
            pass
        monkeypatch.setattr(prepare.asyncio, 'sleep', no_wait)
        uploads, reused, methods, receipts = [], [], [], []
        sequence = 1000
        def handler(request):
            nonlocal sequence
            method = request.url.path.rsplit('/', 1)[-1]
            methods.append(method)
            sequence += 1
            if method in {'sendVideo', 'sendPhoto', 'editMessageMedia'}:
                if request.headers['content-type'].startswith('multipart/'):
                    uploads.append(request.content)
                    file_id = f'cached-{len(uploads)}'
                    kind = 'video' if method == 'sendVideo' else 'photo'
                    message_id = sequence
                else:
                    payload = json.loads(request.content)
                    reused.append(payload)
                    media = payload.get('media', {})
                    kind = media.get('type', 'photo' if method == 'sendPhoto' else 'video')
                    file_id = media.get('media') or payload[kind]
                    message_id = payload.get('message_id', sequence)
                value = {'file_id': file_id}
                return telegram_response({'message_id': message_id, kind: [value] if kind == 'photo' else value})
            return telegram_response(True if method == 'deleteMessage' else {'message_id': sequence, 'file_id': 'valid'})
        cache_path = bot.config.data_dir / CACHE_NAME
        first = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)), video_cache=cache_path)
        first.on_message = lambda *args: receipts.append(args)
        videos = {**{key: (help_video_path(key), 10) for key in HELP_TOPICS}, 'help-cover': (HELP_COVER, 0)}
        report = await prepare.prepare(first, videos, 1)
        await first.close()
        assert report['ready'] and report['uploaded'] == len(videos)
        assert len(uploads) == methods.count('deleteMessage') == len(receipts) == len(videos)
        before = bot.store.all('SELECT * FROM notes')
        second = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        restarted = Bot(bot.config, bot.store, second, None, None)
        try:
            ready = await prepare.prepare(second, videos, 1, check_only=True)
            assert ready['ready'] and ready['uploaded'] == 0
            index = await restarted.show_help(1)
            carrier = restarted.store.one('SELECT message_id FROM bottom_menus WHERE owner=1')['message_id']
            for topic in HELP_TOPICS:
                demo = await restarted.callback(1, None, index['message_id'], f'help:{topic}')
                request = reused[-1]
                payload = request['media']
                assert (payload['width'], payload['height'], payload['duration']) == (1080, 1080, 10)
                assert demo['message_id'] == index['message_id']
                assert payload['caption'] == '\n\n'.join(HELP_TOPICS[topic])
                assert payload['show_caption_above_media'] and payload['supports_streaming']
                assert 'disable_notification' not in request
                assert request['reply_markup'] == {'inline_keyboard': [[{
                    'text': 'Назад', 'callback_data': 'help', 'icon_custom_emoji_id': IDS['back']}]]}
                index = await restarted.callback(1, None, demo['message_id'], 'help')
                assert reused[-1]['media']['caption'] == ''
                assert not reused[-1]['media'].get('caption_entities')
                assert index['message_id'] == demo['message_id']
                assert restarted.bottom_state(1)['screen'] == 'root'
                assert restarted.store.one('SELECT message_id FROM bottom_menus WHERE owner=1')['message_id'] == carrier
            assert len(uploads) == len(videos)
            assert len(reused) == 1 + 2 * len(HELP_TOPICS)
            assert methods.count('deleteMessage') == len(videos)
            assert bot.store.all('SELECT * FROM notes') == before
        finally:
            await second.close()
    asyncio.run(run())


def test_cache_separates_bots_and_changed_files_and_keeps_previous_versions(tmp_path):
    async def run():
        path, db = tmp_path / 'demo.mp4', tmp_path / CACHE_NAME
        path.write_bytes(b'original')
        uploads, reused = [], []
        def handler(request):
            if request.headers['content-type'].startswith('multipart/'):
                uploads.append(request.url.path)
                file_id = f'file-{len(uploads)}'
            else:
                file_id = json.loads(request.content)['video']
                reused.append(file_id)
            return telegram_response({'message_id': len(uploads) + len(reused), 'video': {'file_id': file_id}})
        async def send(token):
            telegram = Telegram(token, httpx.AsyncClient(transport=httpx.MockTransport(handler)), video_cache=db)
            try:
                return await telegram.video(1, path)
            finally:
                await telegram.close()
        await send('123:first')
        await send('123:rotated-token')
        assert len(uploads) == 1 and reused == ['file-1']
        await send('456:another-bot')
        path.write_bytes(b'updated!')
        await send('123:first')
        path.write_bytes(b'original')
        await send('123:first')
        assert len(uploads) == 3 and reused == ['file-1', 'file-1']
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['invalid', 'timeout', 'rate_limit', 'chat', 'emoji'])
def test_cached_delivery_only_reuploads_after_explicit_invalid_reference(tmp_path, failure):
    async def run():
        path = tmp_path / 'demo.mp4'
        path.write_bytes(b'original video')
        cache = VideoFileCache(tmp_path / CACHE_NAME, '123')
        digest = cache.digest(path)
        cache.put(digest, 'old-id')
        requests, receipts = [], []
        def handler(request):
            upload = request.headers['content-type'].startswith('multipart/')
            requests.append('upload' if upload else 'cached')
            if len(requests) == 1:
                if failure == 'timeout':
                    raise httpx.ReadTimeout('lost response')
                code, description = {'invalid': (400, 'Bad Request: wrong file identifier/HTTP URL specified'),
                    'rate_limit': (429, 'Too Many Requests'), 'chat': (400, 'Bad Request: chat not found'),
                    'emoji': (400, 'Bad Request: CUSTOM_EMOJI_ID_INVALID')}[failure]
                return httpx.Response(code, json={'ok': False, 'error_code': code, 'description': description})
            return telegram_response({'message_id': 71, 'video': {'file_id': 'new-id'}})
        telegram = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)), video_cache=cache.path)
        telegram.on_message = lambda *args: receipts.append(args)
        token = silent_delivery.set(True)
        try:
            if failure in {'invalid', 'emoji'}:
                await telegram.video(1, path, 9, caption='📁 Папки', keyboard={
                    'inline_keyboard': [[{'text': '⬅️ Назад', 'callback_data': 'help'}]]})
                assert requests == ['cached', 'upload' if failure == 'invalid' else 'cached']
                assert receipts == [(1, 71, 9)]
            else:
                with pytest.raises(ProviderError):
                    await telegram.video(1, path)
                assert requests == ['cached'] and receipts == []
            assert cache.get(digest) == ('new-id' if failure == 'invalid' else 'old-id')
        finally:
            silent_delivery.reset(token)
            await telegram.close()
    asyncio.run(run())


@pytest.mark.parametrize('cached', [False, True])
@pytest.mark.parametrize('check_only', [False, True])
def test_preparation_checks_or_repairs_missing_and_rejected_ids(tmp_path, cached, check_only, monkeypatch):
    async def run():
        prepare = preparation_module()
        async def no_wait(_):
            pass
        monkeypatch.setattr(prepare.asyncio, 'sleep', no_wait)
        path = tmp_path / 'demo.mp4'
        path.write_bytes(b'video')
        requests = []
        def handler(request):
            method = request.url.path.rsplit('/', 1)[-1]
            requests.append(method)
            if method == 'getFile':
                return httpx.Response(400, json={'ok': False, 'error_code': 400,
                    'description': 'Bad Request: wrong remote file identifier specified'})
            if method == 'sendVideo':
                return telegram_response({'message_id': 71, 'video': {'file_id': 'prepared-id'}})
            return telegram_response(True)
        telegram = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                            video_cache=tmp_path / CACHE_NAME)
        if cached:
            telegram.video_cache.put(telegram.video_cache.digest(path), 'stale-id')
        try:
            if check_only:
                with pytest.raises(RuntimeError, match='video_cache_not_ready:save'):
                    await prepare.prepare(telegram, {'save': (path, 10)}, 1, check_only=True)
                assert requests == (['getFile'] if cached else [])
            else:
                result = await prepare.prepare(telegram, {'save': (path, 10)}, 1)
                assert result['ready'] and result['uploaded'] == 1
                assert telegram.video_cache.get(telegram.video_cache.digest(path)) == 'prepared-id'
                assert requests == (['getFile'] if cached else []) + ['sendVideo', 'deleteMessage']
        finally:
            await telegram.close()
    asyncio.run(run())


def test_preparation_uses_secrets_next_to_configured_app_symlink(tmp_path, monkeypatch):
    module = preparation_module()
    project = tmp_path / 'projects'
    project.mkdir()
    physical = tmp_path / 'storage/application'
    physical.mkdir(parents=True)
    app = project / 'voice_memory_bot'
    app.symlink_to(physical, target_is_directory=True)
    (physical / 'config.json').write_text(json.dumps({'allowed_user_ids': [1]}))
    (project / 'config.json').write_text(json.dumps({
        'my_chat_id': 1, 'bot_token_your_audio_notes': '123:test', 'groq_voice': 'test'}))
    async def check(telegram, videos, owner, **kwargs):
        assert owner == 1 and kwargs['check_only']
        assert telegram.video_cache.path == physical / 'data' / CACHE_NAME
        assert set(videos) == set(HELP_TOPICS) | {'start', 'help-cover'}
        return {'ready': True, 'uploaded': 0}
    monkeypatch.setattr(module, 'prepare', check)
    asyncio.run(module.main(SimpleNamespace(app=app, check=True)))


def test_square_start_preparation_and_cached_delivery_keep_valid_payload(bot, monkeypatch):
    async def run():
        prepare = preparation_module()
        async def no_wait(_):
            pass
        monkeypatch.setattr(prepare.asyncio, 'sleep', no_wait)
        uploads, reused = [], []
        def handler(request):
            if request.url.path.endswith('/sendVideo'):
                if request.headers['content-type'].startswith('multipart/'):
                    uploads.append(request.content)
                else:
                    reused.append(json.loads(request.content))
                return telegram_response({'message_id': 1001, 'video': {'file_id': 'square-video'}})
            return telegram_response(True)
        telegram = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                            video_cache=bot.config.data_dir / CACHE_NAME)
        try:
            await prepare.prepare(telegram, {'start': (prepare.START_VIDEO, 30)}, 1)
            await telegram.video(1, prepare.START_VIDEO, duration=30, width=1080, height=1080,
                                 cover=prepare.START_COVER)
            assert len(uploads) == len(reused) == 1
            assert prepare.START_COVER.read_bytes() in uploads[0]
            assert reused[0]['video'] == 'square-video'
            assert reused[0]['width'] == reused[0]['height'] == 1080
            assert 'cover' not in reused[0]  # No unattached multipart reference in a JSON request.
        finally:
            await telegram.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['invalid', 'timeout', 'not_modified', 'missing', 'emoji'])
def test_help_edits_never_create_or_delete_messages(tmp_path, failure):
    async def run():
        path = tmp_path / 'demo.mp4'
        path.write_bytes(b'video')
        cache = VideoFileCache(tmp_path / CACHE_NAME, '123')
        cache.put(cache.digest(path), 'cached-video')
        requests = []
        def handler(request):
            requests.append(request)
            assert request.url.path.endswith('/editMessageMedia')
            if len(requests) == 1:
                payload = json.loads(request.content)
                assert payload['message_id'] == 42
                assert payload['media']['media'] == 'cached-video'
                assert payload['media']['width'] == payload['media']['height'] == 1080
                assert payload['media']['caption_entities']
                assert payload['reply_markup']['inline_keyboard'][0][0]['icon_custom_emoji_id'] == IDS['back']
                if failure == 'timeout':
                    raise httpx.ReadTimeout('unknown', request=request)
                reasons = {'invalid': 'wrong file identifier', 'not_modified': 'message is not modified',
                           'missing': 'message to edit not found', 'emoji': 'custom emoji unavailable'}
                return httpx.Response(400, json={'ok': False, 'error_code': 400, 'description': reasons[failure]})
            if failure == 'invalid':
                assert request.headers['content-type'].startswith('multipart/')
                assert b'name="message_id"\r\n\r\n42' in request.content
            return telegram_response({'message_id': 42, 'video': {'file_id': 'refreshed'}})
        tg = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)), video_cache=cache.path)
        try:
            async def edit():
                return await tg.help_media(1, path, caption='🎙 Сохранить', edit_id=42,
                    keyboard={'inline_keyboard': [[{'text': '⬅️ Назад', 'callback_data': 'help'}]]})
            if failure in {'timeout', 'missing'}:
                with pytest.raises(ProviderError):
                    await edit()
            else:
                assert (await edit())['message_id'] == 42
            assert len(requests) == (2 if failure in {'invalid', 'emoji'} else 1)
            assert cache.get(cache.digest(path)) == ('refreshed' if failure == 'invalid' else 'cached-video')
        finally:
            await tg.close()
    asyncio.run(run())


def test_preparation_receipts_never_migrate_installed_archive(tmp_path):
    import sqlite3

    from voice_memory_bot.cleanup import CleanupStore
    from voice_memory_bot.config import Limits
    from voice_memory_bot.store import Store

    path = tmp_path / 'archive.sqlite3'
    Store(path, Limits()).close()
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE obsidian_devices')
        db.execute('DROP TABLE obsidian_pairing')
        db.execute('PRAGMA user_version=3')
    receipts = preparation_module().receipt_store(path)
    try:
        CleanupStore(receipts).remember(1, 99)
        assert receipts.db.execute('SELECT message_id FROM telegram_messages').fetchone()[0] == 99
        assert receipts.db.execute('PRAGMA user_version').fetchone()[0] == 3
        assert not receipts.db.execute("SELECT name FROM sqlite_master WHERE name='obsidian_devices'").fetchone()
    finally:
        receipts.close()
