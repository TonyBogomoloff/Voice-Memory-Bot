import asyncio
import hashlib
import json
import zipfile
from dataclasses import replace
from io import BytesIO

import httpx
import pytest
from test_commands import bot as bot
from test_commands import receive
from test_export import ExportTelegram, manifest
from test_folder_shortcuts import labels
from test_mvp import make_bot

from voice_memory_bot.config import Config
from voice_memory_bot.providers import ProviderError, Telegram


def test_options_bind_actions_to_folder_and_survive_restart(bot, tmp_path):
    async def run():
        folder = bot.store.current_folder(1)
        await receive(bot, '⚙️ Options')
        assert {'✏️ Переименовать', '🗑 Удалить папку', '📦 Экспорт',
                '⬅️ Назад'} == set(labels(bot))
        assert bot.store.current_folder(1) == folder
        restarted = make_bot(tmp_path, bot.store)
        assert restarted.options_folder(1) == folder
        await receive(bot, '✏️ Переименовать', 2)
        await receive(bot, 'Мои записи', 3)
        assert bot.store.folder(1, folder)['name'] == 'Мои записи'
        assert '📦 Экспорт' in labels(bot)
        await receive(bot, '🗑 Удалить папку', 4)
        token = bot.folder_delete_confirmations[1][0]
        await bot.callback(1, None, 100, f'canceldrop:{token}')
        assert bot.options_folder(1) == folder
        await receive(bot, '← К папке', 5)
        assert '✓ 📌 Мои записи' in labels(bot)
        await receive(bot, '🗑 Удалить папку', 6)  # Delayed settings button after exit.
        assert bot.folder_delete_confirmations == {}
        assert bot.store.notes(1) == []
    asyncio.run(run())


def test_raw_export_keeps_original_text_caption_names_and_bytes(bot, tmp_path):
    async def run():
        folder = bot.store.current_folder(1)
        text = '  До правки ИИ.\n\nС переносами и пробелами.  '
        await receive(bot, text, 100)
        text_note = bot.store.notes(1)[0]
        with bot.store.db:
            bot.store.db.execute("UPDATE notes SET content='ИИ изменил текст' WHERE id=?", (text_note['id'],))
        originals = {}
        for mid, kind, extension in ((101, 'voice', 'ogg'), (102, 'video_note', 'mp4'),
                                      (103, 'photo', 'jpg'), (104, 'document', 'pdf'), (105, 'video', 'mp4')):
            data = (f'ORIGINAL {kind}').encode()
            media = {'file_id': f'id-{mid}', 'file_size': len(data), 'duration': 1,
                     'file_name': '../../исходник.pdf' if kind == 'document' else f'original.{extension}',
                     'mime_type': 'application/pdf' if kind == 'document' else 'application/octet-stream'}
            event = {'message': {'message_id': mid, 'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'},
                                 'caption': '  Исходная подпись\n  ', kind: [media] if kind == 'photo' else media}}
            await bot.handle(event)
            note = bot.store.one('SELECT * FROM notes WHERE message_id=?', (mid,))
            assert note
            path = bot.archive.audio(note)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            bot.store.update_note(1, note['id'], audio_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            originals[note['id']] = data
        bot.store.ensure_user(2)
        bot.store.add_note(2, bot.store.current_folder(2), {'message_id': 999}, 'text', transcript='OTHER OWNER')
        bot.telegram = ExportTelegram()
        await bot.send_export(1, None, folder, 'raw')
        assert len(bot.telegram.documents) == 1
        with zipfile.ZipFile(BytesIO(bot.telegram.documents[0][1])) as archive:
            meta = manifest(archive)
            assert meta['content'] == 'raw'
            names = archive.namelist()
            assert all(not name.startswith('/') and '..' not in name.split('/') for name in names)
            assert archive.read(next(name for name in names if name.endswith('/text.txt'))).decode() == text
            media = [record for record in meta['files'] if record['note_id'] in originals and '/media/' in record['path']]
            assert len(media) == len(originals)
            for record in media:
                assert archive.read(record['path']) == originals[record['note_id']]
                assert hashlib.sha256(archive.read(record['path'])).hexdigest() == record['sha256']
            assert any(name.endswith('/media/исходник.pdf') for name in names)
            assert len([name for name in names if name.endswith('/caption.txt')]) == 5
            assert all('ИИ изменил текст' not in archive.read(name).decode('utf-8', errors='replace')
                       and 'OTHER OWNER' not in archive.read(name).decode('utf-8', errors='replace') for name in names)
        assert not list((tmp_path / 'exports').iterdir())
    asyncio.run(run())


def test_document_saved_before_source_cleanup_without_asr(bot):
    async def run():
        raw = b'%PDF original file bytes'
        async def download(file_id, path, max_bytes):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            return len(raw), hashlib.sha256(raw).hexdigest()
        bot.telegram.download = download
        event = {'message': {'message_id': 200, 'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'},
                             'document': {'file_id': 'doc-id', 'file_size': len(raw), 'file_name': 'report.pdf'}}}
        await bot.handle(event)
        await bot.clear_folder_view(1, None, all_threads=True)
        assert not any(method == 'deleteMessage' and payload['message_id'] == 200
                       for method, payload in bot.telegram.sent)
        await bot.process(bot.store.claim_note())
        note = bot.store.notes(1)[0]
        assert note['state'] == 'ready' and note['original_name'] == 'report.pdf'
        assert bot.archive.audio(note).read_bytes() == raw
        assert bot.groq.calls == 0
        bot.telegram.sent.clear()
        await bot.clear_folder_view(1, None, all_threads=True)
        assert any(method == 'deleteMessage' and payload['message_id'] == 200
                   for method, payload in bot.telegram.sent)
        assert bot.archive.audio(note).read_bytes() == raw
    asyncio.run(run())


def test_local_download_copies_bytes_and_rejects_outside_root(tmp_path):
    async def run():
        cache = tmp_path / 'cache'
        cache.mkdir()
        source = cache / 'original.ogg'
        source.write_bytes(b'original unchanged')
        target = tmp_path / 'archive' / 'voice.ogg'
        def handler(request):
            return httpx.Response(200, json={'ok': True, 'result': {'file_path': str(source),
                                                                     'file_size': source.stat().st_size}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = Telegram('123:secret', client, api_base='http://127.0.0.1:8083', file_root=cache)
            size, checksum = await api.download('id', target, 100)
            assert size == len(source.read_bytes()) and checksum == hashlib.sha256(source.read_bytes()).hexdigest()
            assert target.read_bytes() == source.read_bytes()
            target.unlink()
            with pytest.raises(ProviderError, match='too_large'):
                await api.download('id', target, 1)
            assert not target.exists() and not target.with_suffix('.part').exists()
            source = tmp_path / 'private.txt'
            source.write_bytes(b'never export')
            with pytest.raises(ProviderError, match='invalid_file_path'):
                await api.download('id', target, 100)
            assert not target.exists()
    asyncio.run(run())


def test_large_raw_zip_uses_local_api_without_cloud_sized_parts(bot, tmp_path):
    async def run():
        note, _ = bot.store.add_note(1, bot.store.current_folder(1), {'message_id': 700},
                                    'file', file_id='large-id', extension='zip', size=51_000_000)
        path = bot.archive.audio(note)
        path.parent.mkdir(parents=True)
        with path.open('wb') as handle:
            handle.truncate(51_000_000)
        with path.open('rb') as handle:
            checksum = hashlib.file_digest(handle, 'sha256').hexdigest()
        bot.store.update_note(1, note['id'], audio_bytes=51_000_000, sha256=checksum)
        requests = []
        def handler(request):
            requests.append((request.url.host, request.url.port, request.url.path.rsplit('/', 1)[-1]))
            if request.url.path.endswith('sendDocument'):
                assert len(request.content) > 50_000_000
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 123}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            bot.telegram = Telegram('123:secret', client, api_base='http://127.0.0.1:8083', file_root=tmp_path)
            bot.config = replace(bot.config, limits=replace(bot.config.limits, export_part_bytes=1_950_000_000))
            await bot.send_export(1, None, bot.store.current_folder(1), 'raw')
        assert requests.count(('127.0.0.1', 8083, 'sendDocument')) == 1
    asyncio.run(run())


def test_config_allows_large_exports_only_with_local_api(tmp_path):
    path = tmp_path / 'config.json'
    values = {'bot_token': '123:secret', 'groq_voice': 'secret', 'allowed_user_ids': [1],
              'limits': {'max_file_bytes': 100_000_000, 'export_part_bytes': 1_950_000_000}}
    path.write_text(json.dumps(values))
    with pytest.raises(ValueError, match='облачного'):
        Config.load(path)
    values.update(telegram_api_base='http://127.0.0.1:8083', telegram_file_root=str(tmp_path))
    path.write_text(json.dumps(values))
    assert Config.load(path).limits.export_part_bytes == 1_950_000_000
