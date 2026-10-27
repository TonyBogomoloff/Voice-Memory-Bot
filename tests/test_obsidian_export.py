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
from voice_memory_bot.obsidian_settings import ObsidianSettings


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


def test_folder_history_is_ordered_with_photos_and_optional_audio(tmp_path, store):
    archive = Archive(tmp_path / 'notes')
    voice = add(store, kind='voice', file_id='SECRET_FILE_ID', extension='ogg', transcript='Расшифровка')
    photo = add(store, message=2, kind='photo', file_id='SECRET_PHOTO_ID', extension='jpg', transcript='Подпись фото')
    text = add(store, message=3, kind='text', transcript='[[Ссылка]]\n- [ ] Задача')
    add(store, owner=2, kind='text', transcript='ЧУЖОЙ СЕКРЕТ')
    media = {voice['id']: b'original-audio' * 200, photo['id']: b'original-photo' * 60}
    for note in (voice, photo):
        path = archive.audio(note)
        path.parent.mkdir(parents=True)
        path.write_bytes(media[note['id']])
        store.update_note(1, note['id'], state='ready', audio_bytes=len(media[note['id']]),
                          sha256=hashlib.sha256(media[note['id']]).hexdigest())
    store.update_note(1, voice['id'], title='../../Идея: "Ёж"')
    with store.transaction():
        store.db.execute('UPDATE notes SET content=? WHERE id=?', ('Исправленный **текст**', voice['id']))
        for n, note in enumerate((text, voice, photo)):
            store.db.execute('UPDATE notes SET created_at=?,state="ready" WHERE id=?', (1000 + n, note['id']))
    assert ObsidianSettings(store).audio(1, voice['folder_id'])
    ObsidianSettings(store).set_audio(1, 'all', False)
    snapshot = store.export_snapshot(1)
    first = archive.export(snapshot.notes, snapshot.folders, tmp_path / 'one', True, 6500,
                           obsidian=True, settings=snapshot.settings)
    files, records = unpack(first)
    documents = [r for r in records if r['kind'] == 'note']
    assert {r['note_id'] for r in documents} == {f['id'] for f in snapshot.folders}
    row = next(r for r in documents if r['folder_id'] == voice['folder_id'])
    assert row['layout'] == 'folder' and [a['note_id'] for a in row['attachments']] == [photo['id']]
    markdown = files[row['path']].decode()
    assert markdown.index('[[Ссылка]]') < markdown.index('Исправленный **текст**') < markdown.index('Подпись фото')
    assert 'Расшифровка' in markdown and 'Мои дополнения' in markdown
    relative = unquote(markdown.split('![Фото](', 1)[1].split(')', 1)[0])
    assert files[relative] == media[photo['id']]
    assert not any(path.endswith('.ogg') for path in files)
    ObsidianSettings(store).set_audio(1, voice['folder_id'], True)
    selected = store.export_snapshot(1)
    audio = archive.export(selected.notes, selected.folders, tmp_path / 'two', True, 6500,
                           obsidian=True, settings=selected.settings)
    repeated = archive.export(selected.notes, selected.folders, tmp_path / 'three', True, 6500,
                              obsidian=True, settings=selected.settings)
    audio_files, _ = unpack(audio)
    assert audio_files == unpack(repeated)[0] and len(audio) > 1
    assert any(data == media[voice['id']] for data in audio_files.values())
    assert not snapshot.settings['obsidian_audio'][voice['folder_id']]  # Frozen preference.
    assert all('..' not in p.split('/') and not p.startswith('/') for p in audio_files)
    assert all(b'SECRET' not in data and 'ЧУЖОЙ'.encode() not in data for data in audio_files.values())


def test_bad_media_cleans_partial_export_and_mixed_owners_are_rejected(tmp_path, store):
    archive = Archive(tmp_path / 'notes')
    note = add(store, kind='photo', file_id='private', extension='jpg', transcript='Подпись')
    media = archive.audio(note)
    media.parent.mkdir(parents=True)
    media.write_bytes(b'bad')
    store.update_note(1, note['id'], state='ready', audio_bytes=3, sha256=hashlib.sha256(b'old').hexdigest())
    with pytest.raises(ValueError, match='checksum'):
        archive.export(store.export_notes(1), store.folders(1), tmp_path / 'bad', True, 45000000, obsidian=True)
    assert not list((tmp_path / 'bad').glob('*.zip'))
    with pytest.raises(ValueError, match='owner_mismatch'):
        archive.export(store.export_notes(1), store.folders(1) + store.folders(2),
                       tmp_path / 'mixed', False, 45000000, obsidian=True)


def test_telegram_obsidian_export_and_folder_scope(tmp_path, store):
    note = add(store, kind='text', transcript='Моя заметка')
    store.update_note(1, note['id'], state='ready')
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
