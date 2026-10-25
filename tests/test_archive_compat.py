import hashlib
import zipfile

import pytest

from voice_memory_bot.ai_store import AIStore
from voice_memory_bot.archive import Archive
from voice_memory_bot.config import Limits
from voice_memory_bot.message_store import MessageStore
from voice_memory_bot.store import Store


@pytest.mark.parametrize('kind,extension,mime', [
    ('voice', 'webm', 'audio/webm'),
    ('photo', 'png', 'image/png'),
    ('document', 'pdf', 'application/pdf'),
])
def test_saved_uploads_survive_reopen_move_search_and_export(tmp_path, kind, extension, mime):
    path = tmp_path / 'archive.sqlite3'
    archive = Archive(tmp_path / 'notes')
    store = Store(path, Limits())
    try:
        folder = store.ensure_user(1)
        destination = store.create_folder(1, 'Другая')
        note, _ = store.add_note(1, folder, {'message_id': -100}, kind,
                                 extension=extension, transcript='Сохранённая астролябия')
        media = b'preserved original bytes'
        store.update_note(1, note['id'], state='ready', origin='web',
                          original_name='original.' + extension, mime_type=mime,
                          audio_bytes=len(media), sha256=hashlib.sha256(media).hexdigest(),
                          routing='manual', delivery='sent', transcript_delivery='sent')
        # These columns and identifiers already exist in older archives.
        with store.db:
            store.db.execute('ALTER TABLE notes ADD COLUMN upload_key TEXT')
            store.db.execute('ALTER TABLE notes ADD COLUMN ai_context TEXT')
            store.db.execute('CREATE UNIQUE INDEX web_upload_key ON notes(owner,upload_key) '
                             'WHERE upload_key IS NOT NULL')
            store.db.execute('UPDATE notes SET upload_key=?,ai_context=? WHERE id=?',
                             ('saved-key', note['id'], note['id']))
        before = store.note(1, note['id'])
        original = archive.audio(before)
        original.parent.mkdir(parents=True)
        original.write_bytes(media)
        archive.persist_text(before, store.folder(1, folder))
    finally:
        store.close()

    store = Store(path, Limits())
    try:
        assert store.note(1, note['id']) == before
        assert store.notes(1, query='астролябия')[0]['id'] == note['id']
        assert store.notes(2, query='астролябия') == []
        MessageStore(store).move_archive(store.note(1, note['id']), destination['id'])
        moved = store.note(1, note['id'])
        assert archive.audio(moved) == original and original.read_bytes() == media
        parts = archive.export(store.export_notes(1), store.folders(1), tmp_path / 'export',
                               True, 45_000_000, text_format='markdown')
        with zipfile.ZipFile(parts[0]) as exported:
            assert media in [exported.read(name) for name in exported.namelist()]
            text = ''.join(exported.read(name).decode() for name in exported.namelist()
                           if name.endswith('history.md'))
            assert before['transcript'] in text
        state = AIStore(store)
        state.enqueue(1, note['id'], 'button')
        state.finish_reply(1, note['id'], 'Ответ на сохранённую запись')
        answer = store.note(1, state.request(1, note['id'])['result_note_id'])
        assert answer['folder_id'] == destination['id']
        assert answer['transcript'] == 'Ответ на сохранённую запись'
        assert store.note(1, note['id'])['transcript'] == before['transcript']
    finally:
        store.close()
