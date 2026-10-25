import pytest
from test_mvp import add
from test_mvp import store as store

from voice_memory_bot.message_store import MessageStore


def test_move_and_receipt_rollback_with_outer_operation(store):
    note = add(store, kind='text', transcript='Сохранить')
    messages = MessageStore(store)
    with pytest.raises(RuntimeError, match='receipt failed'):
        with store.transaction():
            folder = store.create_folder(1, 'Временная')
            messages.move_archive(note, folder['id'])
            raise RuntimeError('receipt failed')
    assert store.note(1, note['id'])['folder_id'] == note['folder_id']
    assert messages.get(1, note['id']) is None
    assert not any(f['name'] == 'Временная' for f in store.folders(1))
    assert not store.db.in_transaction


def test_nested_rollback_does_not_abort_siblings(store):
    with store.transaction():
        store.create_folder(1, 'Первая')
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.create_folder(1, 'Отменённая')
                raise RuntimeError('cancel nested operation')
        store.create_folder(1, 'Вторая')
    names = {f['name'] for f in store.folders(1)}
    assert {'Первая', 'Вторая'} <= names
    assert 'Отменённая' not in names
    assert not store.db.in_transaction
