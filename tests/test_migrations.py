import sqlite3

import pytest
from support.base import add, make_bot

from voice_memory_bot import migrations
from voice_memory_bot.ai_store import AIStore
from voice_memory_bot.config import Limits
from voice_memory_bot.message_store import MessageStore
from voice_memory_bot.store import Store


def test_open_and_component_construction_preserve_inflight_until_runtime_recovery(tmp_path):
    path = tmp_path / 'db'
    store = Store(path, Limits())
    note = add(store, kind='text', transcript='Полный исходный текст')
    ai = AIStore(store)
    ai.enqueue(1, note['id'], 'button')
    ai.update_request(1, note['id'], state='running', delivery='sending')
    store.update_note(1, note['id'], state='processing', delivery='sending', transcript_delivery='sending')
    MessageStore(store).begin(note, note['folder_id'], None)
    with store.transaction():
        store.db.execute("INSERT INTO updates VALUES (1,'{}','processing')")
        store.db.execute("INSERT INTO updates VALUES (2,'{}','failed')")
    before = store.note(1, note['id'])
    store.close()
    store = Store(path, Limits())
    try:
        store.db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action in {
            sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_DROP_TABLE
        } else sqlite3.SQLITE_OK)
        make_bot(tmp_path, store)
        assert store.note(1, note['id']) == before
        assert AIStore(store).request(1, note['id'])['state'] == 'running'
        store.recover_runtime()
        saved = store.note(1, note['id'])
        assert saved['state'] == 'queued' and saved['delivery'] == saved['transcript_delivery'] == 'unknown'
        assert saved['transcript'] == before['transcript']
        assert AIStore(store).request(1, note['id'])['delivery'] == 'unknown'
        assert MessageStore(store).get(1, note['id'])['state'] == 'unknown'
        assert store.updates.counts() == {'pending': 1, 'processing': 0, 'failed': 1}
    finally:
        store.close()


def test_failed_legacy_migration_rolls_back_schema_and_version(tmp_path, monkeypatch):
    path = tmp_path / 'db'
    store = Store(path, Limits())
    note = add(store, kind='text', transcript='Не потерять')
    store.db.execute('ALTER TABLE notes DROP COLUMN transcript_message_id')
    store.db.execute('PRAGMA user_version=0')
    store.close()

    def fail(db):
        raise RuntimeError('migration interrupted')

    monkeypatch.setattr(migrations, '_migrate_cleanup', fail)
    with pytest.raises(RuntimeError, match='migration interrupted'):
        Store(path, Limits())
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0
        assert 'transcript_message_id' not in {r[1] for r in db.execute('PRAGMA table_info(notes)')}
        assert db.execute('SELECT transcript FROM notes WHERE id=?', (note['id'],)).fetchone()[0] == 'Не потерять'



def test_page_settings_upgrade_preserves_archive_and_resets_old_offsets(tmp_path):
    path = tmp_path / 'db'
    store = Store(path, Limits())
    note = add(store, kind='text', transcript='Сохранённый текст')
    before = store.note(1, note['id'])
    with store.transaction():
        store.db.execute('DROP TABLE user_view_settings')
        store.db.execute('PRAGMA user_version=1')
        store.db.execute('INSERT INTO folder_views(owner,folder_id,page,dirty) VALUES (?,?,4,0)',
                         (1, note['folder_id']))
    store.close()
    store = Store(path, Limits())
    try:
        assert store.folder_page_size(1) == 20
        assert store.note(1, note['id']) == before
        assert store.one('SELECT page,dirty FROM folder_views WHERE owner=1') == {'page': 0, 'dirty': 0}
        store.set_folder_page_size(1, 100)
        assert store.folder_page_size(1) == 100
    finally:
        store.close()


def test_ai_instruction_prompt_upgrade_from_installed_v2_preserves_archive(tmp_path):
    path = tmp_path / 'db'
    store = Store(path, Limits())
    note = add(store, kind='text', transcript='Исходная запись')
    before = store.note(1, note['id'])
    with store.transaction():
        store.db.execute('DROP TABLE ai_instruction_prompts')
        store.db.execute('PRAGMA user_version=2')
    store.close()
    store = Store(path, Limits())
    try:
        assert store.note(1, note['id']) == before
        conversation = AIStore(store).start_note_conversation(1, note['id'])
        with store.transaction():
            store.db.execute('INSERT INTO ai_instruction_prompts VALUES (?,?,?,?)',
                             (conversation, 1, 20, note['message_id']))
    finally:
        store.close()
    store = Store(path, Limits())
    try:
        assert store.one('SELECT message_id,reply_to FROM ai_instruction_prompts') == {
            'message_id': 20, 'reply_to': note['message_id']}
        assert store.note(1, note['id']) == before
    finally:
        store.close()



def test_trash_upgrade_preserves_existing_records_and_reuses_namesake(tmp_path):
    path = tmp_path / 'db'
    store = Store(path, Limits())
    trash = store.trash_folder(1)
    note, _ = store.add_note(1, trash['id'], {'message_id': 20}, 'text', transcript='Сохранить оригинал')
    store.update_note(1, note['id'], state='ready', delivery='sent', transcript_delivery='sent')
    before = store.note(1, note['id'])
    with store.transaction():
        store.db.execute('DROP INDEX one_trash_per_owner')
        store.db.execute('ALTER TABLE folders DROP COLUMN is_trash')
        store.db.execute('PRAGMA user_version=4')
    old_folder = store.folder(1, trash['id'])
    store.close()
    store = Store(path, Limits())
    try:
        migrated = store.folder(1, trash['id'])
        assert {k: migrated[k] for k in old_folder} == old_folder
        assert store.note(1, note['id']) == before
        store.recover_runtime()
        assert store.trash_folder(1)['id'] == trash['id']
        assert store.note(1, note['id']) == before
        assert len(store.folders(1)) == 1
        assert store.all('PRAGMA foreign_key_check') == []
    finally:
        store.close()
