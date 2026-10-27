import asyncio
import hashlib
import time
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from support.exports import ExportTelegram

from voice_memory_bot.bot import Bot
from voice_memory_bot.config import Config
from voice_memory_bot.obsidian_access import AccessError, ObsidianAccess, digest
from voice_memory_bot.obsidian_api import ExportAPI
from voice_memory_bot.trash import TrashStore


def bot_for(store, tmp_path):
    return Bot(Config('123:secret', frozenset({1, 2}), 'secret', tmp_path,
                      obsidian_url='https://audio-notes.korobkoff.com'),
               store, ExportTelegram(), None, None)


def add(store, owner=1, message=1, **kwargs):
    return store.add_note(owner, store.ensure_user(owner), {'message_id': message},
                          kind='text', transcript='Заметка', **kwargs)[0]


def test_pairing_expiry_single_use_owner_revocation_and_scope(store, tmp_path):
    access = ObsidianAccess(store)
    folder = store.ensure_user(1)
    code = access.issue(1, folder)
    result = access.pair(code, 'Мой iPhone', {1})
    assert access.authenticate(result['token'], {1})['folder_id'] == folder
    with pytest.raises(AccessError):
        access.pair(code, 'Повтор', {1})
    with pytest.raises(AccessError):
        access.authenticate(result['token'], {2})
    access.revoke(2, result['device_id'])
    assert access.authenticate(result['token'], {1})
    access.revoke(1, result['device_id'])
    with pytest.raises(AccessError):
        access.authenticate(result['token'], {1})
    expired = access.issue(1)
    with store.transaction():
        store.db.execute('UPDATE obsidian_pairing SET expires_at=?', (time.time() - 1,))
    with pytest.raises(AccessError):
        access.pair(expired, 'Android', {1})
    assert store.one('SELECT code_hash FROM obsidian_pairing')['code_hash'] == digest(expired)


def test_api_roundtrip_is_scoped_read_only_and_verifies_immutable_media(store, tmp_path):
    bot = bot_for(store, tmp_path)
    note = store.add_note(1, store.ensure_user(1), {'message_id': 1}, kind='voice',
                          extension='ogg', file_id='SECRET_FILE_ID', transcript='Привет')[0]
    media = b'original audio bytes'
    path = bot.archive.audio(note)
    path.parent.mkdir(parents=True)
    path.write_bytes(media)
    store.update_note(1, note['id'], state='ready', audio_bytes=len(media), sha256=hashlib.sha256(media).hexdigest())
    private = add(store, owner=2)
    other_folder = store.create_folder(1, 'Другой проект')['id']
    other = add(store, message=3)
    store.update_note(1, other['id'], folder_id=other_folder, state='ready')
    access = ObsidianAccess(store)
    code = access.issue(1, note['folder_id'])

    async def run():
        async with TestClient(TestServer(ExportAPI(bot).application())) as client:
            assert (await client.get('/v1/notes')).status == 401
            response = await client.post('/v1/pair', json={'code': code, 'name': 'Android'})
            paired = await response.json()
            headers = {'Authorization': 'Bearer ' + paired['token']}
            assert (await client.post('/v1/pair', json={'code': code, 'name': 'iOS'})).status == 401
            listing = await client.get('/v1/notes', headers=headers)
            assert listing.headers['Cache-Control'] == 'no-store'
            records = (await listing.json())['notes']
            assert [r['note_id'] for r in records] == [note['id']]
            row = records[0]
            doc = await client.get(f"/v1/notes/{note['id']}/document?sha256={row['sha256']}", headers=headers)
            data = await doc.read()
            assert doc.status == 200 and hashlib.sha256(data).hexdigest() == row['sha256']
            assert b'SECRET_FILE_ID' not in data
            attachment = row['attachments'][0]
            url = f"/v1/notes/{note['id']}/media?sha256={attachment['sha256']}"
            response = await client.get(url, headers=headers)
            assert await response.read() == media
            for hidden in [private, other]:
                assert (await client.get(f"/v1/notes/{hidden['id']}/document", headers=headers)).status == 404
            assert (await client.put(f"/v1/notes/{note['id']}/document", data='overwrite', headers=headers)).status == 405
            store.update_note(1, note['id'], title='Изменено')
            assert (await client.get(f"/v1/notes/{note['id']}/document?sha256={row['sha256']}", headers=headers)).status == 409
            path.write_bytes(b'x' * len(media))
            assert (await client.get(url, headers=headers)).status == 409
            access.revoke(1, paired['device_id'])
            assert (await client.get('/v1/notes', headers=headers)).status == 401

    asyncio.run(run())


def test_pending_notes_pagination_and_pairing_rate_limit(store, tmp_path):
    bot = bot_for(store, tmp_path)
    for message in range(1, 54):
        n = add(store, message=message)
        store.update_note(1, n['id'], state='ready' if message != 1 else 'processing')
    access = ObsidianAccess(store)
    pair = access.pair(access.issue(1), 'Desktop', {1})
    headers = {'Authorization': 'Bearer ' + pair['token']}

    async def run():
        async with TestClient(TestServer(ExportAPI(bot).application())) as client:
            first = await (await client.get('/v1/notes', headers=headers)).json()
            second = await (await client.get('/v1/notes?cursor=' + first['next_cursor'], headers=headers)).json()
            ids = [r['note_id'] for r in first['notes'] + second['notes']]
            assert len(set(ids)) == len(ids) == 52 and second['next_cursor'] is None
            for _ in range(11):
                response = await client.post('/v1/pair', json={'code': 'bad', 'name': 'phone'})
            assert response.status == 429 and response.headers['Retry-After'] == '60'

    asyncio.run(run())


def test_telegram_pairing_menu_navigation_and_disabled_mode(store, tmp_path):
    bot = bot_for(store, tmp_path)

    async def run():
        await bot.callback(1, None, 50, 'obsidianpair:all')
        state = bot.bottom_state(1)
        assert state['screen'] == 'obsidian:all'
        assert '10 минут' in state['text']
        assert state['back'] == 'settings:all'
        access = ObsidianAccess(store)
        for _ in range(2):
            access.pair(access.issue(1), 'iPhone', {1})
        await bot.obsidian_menu(1, None)
        actions = bot.bottom_state(1)['actions']
        assert len([name for name in actions if name.startswith('Отключить')]) == 2
        bot.config = replace(bot.config, obsidian_url='')
        await bot.obsidian_menu(1, None)
        assert not any('Подключить' in name for name in bot.bottom_state(1)['actions'])
        with pytest.raises(ValueError):
            await bot.obsidian_callback(1, None, 50, 'obsidianpair:all')

    asyncio.run(run())


def test_clear_trash_preserves_pairing_and_devices_for_all_owners(store):
    access = ObsidianAccess(store)
    for owner in (1, 2):
        store.ensure_user(owner)
    first = access.pair(access.issue(1), 'Phone', {1, 2})
    second = access.pair(access.issue(2), 'Phone', {1, 2})
    pending = {owner: access.issue(owner) for owner in (1, 2)}
    trash = store.trash_folder(1)['id']
    note, _ = store.add_note(1, trash, {'message_id': 1000}, 'text', transcript='В корзине')
    store.update_note(1, note['id'], state='ready', delivery='sent', transcript_delivery='sent')
    TrashStore(store).begin(1, trash, [note['id']])
    assert not store.one('SELECT 1 FROM notes WHERE id=?', (note['id'],))
    assert access.authenticate(first['token'], {1, 2})['owner'] == 1
    assert access.pair(pending[1], 'New phone', {1, 2})['token']
    assert access.authenticate(second['token'], {1, 2})['owner'] == 2
    assert access.pair(pending[2], 'New phone', {1, 2})['token']
