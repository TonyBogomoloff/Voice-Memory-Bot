import asyncio
import hashlib
import json
import zipfile
from urllib.parse import unquote

import pytest
from support.exports import ExportTelegram

from voice_memory_bot.archive import Archive
from voice_memory_bot.bot import Bot
from voice_memory_bot.config import Config


def add(store, owner=1, message=1, **values):
    return store.add_note(owner, store.ensure_user(owner), {"message_id": message}, **values)[0]


def unpack(paths):
    files, records = {}, []
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.startswith('manifest-'):
                    manifest = json.loads(archive.read(name))
                    assert manifest['format'] == 'voice-memory-obsidian'
                    records.extend(manifest['files'])
                elif not name.endswith('/'):
                    files[name] = archive.read(name)
    for record in records:
        assert len(files[record['path']]) == record['bytes']
        assert hashlib.sha256(files[record['path']]).hexdigest() == record['sha256']
    return files, records


def test_portable_notes_media_and_originals_are_lossless_and_owner_scoped(tmp_path, store):
    archive = Archive(tmp_path / 'notes')
    note = add(store, kind='voice', file_id='SECRET_FILE_ID', extension='ogg', transcript='  Исходный\nтекст  ')
    media = b'original-audio' * 200
    path = archive.audio(note)
    path.parent.mkdir(parents=True)
    path.write_bytes(media)
    store.update_note(1, note['id'], title='../../Идея: "Ёж"',
                      audio_bytes=len(media), sha256=hashlib.sha256(media).hexdigest())
    other = add(store, message=2, kind='text', transcript='[[Ссылка]]\n- [ ] Задача')
    add(store, owner=2, kind='text', transcript='ЧУЖОЙ СЕКРЕТ')
    snapshot = store.export_snapshot(1)
    next(n for n in snapshot.notes if n['id'] == note['id'])['content'] = 'Исправленный **текст**'
    first = archive.export(snapshot.notes, snapshot.folders, tmp_path / 'one', True, 4800, obsidian=True)
    second = archive.export(snapshot.notes, snapshot.folders, tmp_path / 'two', True, 4800, obsidian=True)
    files, records = unpack(first)
    assert files == unpack(second)[0]
    assert len(first) > 1 and all(p.stat().st_size <= 4800 for p in first)
    documents = [r for r in records if r['kind'] == 'note']
    assert {r['note_id'] for r in documents} == {note['id'], other['id']}
    row = next(r for r in documents if r['note_id'] == note['id'])
    markdown = files[row['path']].decode()
    assert 'voice_memory_id: "' + note['id'] + '"' in markdown
    assert 'Исправленный **текст**' in markdown and '  Исходный\nтекст  ' in markdown
    relative = unquote(markdown.split('![Исходное аудио](', 1)[1].split(')', 1)[0])
    assert files[relative.removeprefix('../')] == media
    assert all('..' not in p.split('/') and not p.startswith('/') for p in files)
    assert all(b'SECRET' not in data and 'ЧУЖОЙ'.encode() not in data for data in files.values())


def test_bad_media_cleans_partial_export_and_mixed_owners_are_rejected(tmp_path, store):
    archive = Archive(tmp_path / 'notes')
    note = add(store, kind='photo', file_id='private', extension='jpg', transcript='Подпись')
    media = archive.audio(note)
    media.parent.mkdir(parents=True)
    media.write_bytes(b'bad')
    store.update_note(1, note['id'], audio_bytes=3, sha256=hashlib.sha256(b'old').hexdigest())
    with pytest.raises(ValueError, match='checksum'):
        archive.export(store.export_notes(1), store.folders(1), tmp_path / 'bad', True, 45000000, obsidian=True)
    assert not list((tmp_path / 'bad').glob('*.zip'))
    with pytest.raises(ValueError, match='owner_mismatch'):
        archive.export(store.export_notes(1), store.folders(1) + store.folders(2),
                       tmp_path / 'mixed', False, 45000000, obsidian=True)


def test_telegram_obsidian_export_and_folder_scope(tmp_path, store):
    add(store, kind='text', transcript='Моя заметка')
    private = store.create_folder(2, 'Чужая папка')['id']

    async def run():
        telegram = ExportTelegram()
        bot = Bot(Config('123:secret', frozenset({1}), 'secret', tmp_path), store, telegram, None, None)
        await bot.export_menu(1, None, 'all')
        assert any('Obsidian' in label for label in bot.bottom_state(1)['actions'])
        await bot.callback(1, None, 50, 'zip:all:obsidian')
        await asyncio.gather(*bot.export_tasks)
        assert telegram.documents
        with pytest.raises(ValueError):
            store.export_snapshot(1, private)

    asyncio.run(run())
