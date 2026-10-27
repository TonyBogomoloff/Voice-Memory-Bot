import asyncio
import time

import pytest
from support.base import make_bot, receive_voice


def test_folder_deletion_keeps_files_notes_versions_ai_history_and_other_users(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        folder = store.create_folder(1, "Проект", 100)
        other = store.create_folder(2, "Проект", 200)
        store.select(1, folder["id"])
        await receive_voice(bot)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        bot.ai_state._snapshot(note)
        conversation = bot.ai_state.active(1, folder["id"])
        original = bot.archive.audio(note).read_bytes()
        transcript = note["transcript"]
        bot.telegram.sent.clear()
        inbox = store.delete_folder(1, folder["id"])
        bot.flush_ai_archive()
        saved = store.note(1, note["id"])
        trash = store.trash_folder(1)
        assert saved["folder_id"] == trash['id']
        assert saved["source_folder"] == folder["id"]
        assert store.current_folder(1) == inbox
        assert store.folder(1, folder["id"])["removed"]
        assert folder["id"] not in {f["id"] for f in store.folders(1)}
        assert saved["transcript"] == transcript and bot.archive.audio(saved).read_bytes() == original
        assert bot.ai_state.conversation(1, conversation)["folder_id"] == trash['id']
        assert store.one("SELECT folder_id FROM note_revisions WHERE note_id=?", (note["id"],))["folder_id"] == folder["id"]
        assert store.folder(2, other["id"])["thread_id"] == 200
        assert store.is_implicit_topic(1, 100)
        assert store.all("PRAGMA foreign_key_check") == []
        assert store.one("PRAGMA quick_check")["quick_check"] == "ok"
        assert "Проект: Корзина" in (bot.archive.directory(saved) / "note.md").read_text()
        assert bot.telegram.sent == []
    asyncio.run(run())


def test_delete_folder_confirmation_is_owner_scoped_cancelable_expiring_and_blocks_busy_work(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        folder = store.create_folder(1, "Работа")
        other = store.create_folder(2, "Личное")
        await bot.show_notes_menu(1, None, folder['id'])
        from support.menus import reply_keyboard
        before = reply_keyboard(bot)
        state = bot.bottom_state(1)
        carrier = store.one('SELECT message_id FROM bottom_menus WHERE owner=1')['message_id']
        sent = len(bot.telegram.sent)
        await bot.callback(1, None, 500, f"dropfolder:{folder['id']}")
        token = bot.folder_delete_confirmations[1][0]
        assert reply_keyboard(bot) == before and bot.bottom_state(1) == state
        assert len(bot.telegram.sent[sent:]) == 1
        assert not any(method == 'deleteMessage' and payload['message_id'] == carrier
                       for method, payload in bot.telegram.sent[sent:])
        prompt = next(p for m, p in reversed(bot.telegram.sent) if m == "sendMessage")
        assert 'переместятся в «Корзину»' in prompt['text']
        assert prompt['keyboard']['inline_keyboard'] == [[
            {'text': 'Удалить', 'callback_data': f'confirmdrop:{token}', 'style': 'danger'},
            {'text': 'Отмена', 'callback_data': f'canceldrop:{token}'}]]
        assert store.folder(1, folder["id"])
        with pytest.raises(ValueError, match="устарело"):
            await bot.callback(2, None, 500, f"confirmdrop:{token}")
        await bot.callback(1, None, 500, f"canceldrop:{token}")
        assert reply_keyboard(bot) == before and bot.bottom_state(1) == state
        assert not store.folder(1, folder['id'])['removed']
        with pytest.raises(ValueError):
            await bot.callback(1, None, 500, f"confirmdrop:{token}")
        with pytest.raises(ValueError):
            store.delete_folder(1, other["id"])
        await bot.callback(1, None, 500, f"dropfolder:{folder['id']}")
        token, identity, name, _ = bot.folder_delete_confirmations[1]
        bot.folder_delete_confirmations[1] = (token, identity, name, time.monotonic() - 1)
        with pytest.raises(ValueError, match="устарело"):
            await bot.callback(1, None, 500, f"confirmdrop:{token}")
        note, _ = store.add_note(1, folder["id"], {"message_id": 10}, "text", transcript="Сохранить")
        with pytest.raises(ValueError, match="завершения"):
            store.delete_folder(1, folder["id"])
        store.update_note(1, note["id"], state="ready", delivery="sent")
        await bot.callback(1, None, 500, f"dropfolder:{folder['id']}")
        token = bot.folder_delete_confirmations[1][0]
        await bot.callback(1, None, 500, f"confirmdrop:{token}")
        assert store.folder(1, folder["id"])["removed"]
        assert store.note(1, note["id"])["folder_id"] == store.trash_folder(1)['id']
        with pytest.raises(ValueError):
            await bot.callback(1, None, 500, f"confirmdrop:{token}")
    asyncio.run(run())


@pytest.mark.parametrize('screen', ['root', 'foldermenu'])
def test_deletion_prompt_preserves_working_lower_buttons(tmp_path, store, screen):
    from support.commands import receive
    from support.menus import reply_keyboard

    async def run():
        bot = make_bot(tmp_path, store)
        folder = store.create_folder(1, 'Работа')
        store.select(1, folder['id'])
        if screen == 'root':
            await bot.show_home(1)
        else:
            await bot.show_folder_options(1, identity=folder['id'])
        keyboard = reply_keyboard(bot)
        state = bot.bottom_state(1)
        carrier = store.one('SELECT message_id FROM bottom_menus WHERE owner=1')['message_id']
        before = len(bot.telegram.sent)
        await bot.callback(1, None, carrier, f"dropfolder:{folder['id']}")
        assert bot.bottom_state(1) == state and reply_keyboard(bot) == keyboard
        assert [method for method, _ in bot.telegram.sent[before:]] == ['sendMessage']
        if screen == 'root':
            await receive(bot, '📁 Заметки', 1001)
            assert store.current_folder(1) == store.ensure_user(1)
        else:
            await receive(bot, '✏️ Переименовать', 1001)
            assert store.action(1) == 'rename:' + folder['id']
        assert not store.folder(1, folder['id'])['removed']
        assert store.notes(1) == []
    asyncio.run(run())


def test_trash_is_visible_empty_protected_and_does_not_take_folder_quota(tmp_path):
    from voice_memory_bot.config import Limits
    from voice_memory_bot.store import Store

    store = Store(tmp_path / 'db', Limits(max_folders_per_user=1))
    try:
        store.register_user(1)
        trash = store.trash_folder(1)
        assert [(f['name'], f['count']) for f in store.folders(1)] == [('Заметки', 0), ('Корзина', 0)]
        folder = store.folder(1, store.ensure_user(1))
        store.rename_folder(1, folder['id'], 'Работа')
        with pytest.raises(ValueError, match='лимит'):
            store.create_folder(1, 'Ещё')
        for operation in (lambda: store.delete_folder(1, trash['id']),
                          lambda: store.rename_folder(1, trash['id'], 'Другое')):
            with pytest.raises(ValueError, match='Системную'):
                operation()
        assert store.trash_folder(2)['id'] != trash['id']
        with pytest.raises(ValueError, match='Последнюю'):
            store.delete_folder(1, folder['id'])
        assert store.current_folder(1) == folder['id']
        assert store.trash_folder(1)['id'] == trash['id']
    finally:
        store.close()


def test_folder_deletion_is_atomic_and_trashed_note_can_be_restored(tmp_path, store):
    import sqlite3

    async def run():
        bot = make_bot(tmp_path, store)
        folder = store.create_folder(1, 'Работа')
        note, _ = store.add_note(1, folder['id'], {'message_id': 101}, 'text', transcript='Не потерять')
        store.update_note(1, note['id'], state='ready', delivery='sent', transcript_delivery='sent')
        note = store.note(1, note['id'])
        conversation = bot.ai_state.active(1, folder['id'])
        # A storage error at the last step must roll back records and AI together.
        with store.transaction():
            store.db.execute("CREATE TRIGGER stop_folder_removal BEFORE UPDATE OF removed ON folders "
                             "WHEN NEW.removed=1 BEGIN SELECT RAISE(ABORT, 'disk error'); END")
        with pytest.raises(sqlite3.IntegrityError, match='disk error'):
            store.delete_folder(1, folder['id'])
        assert store.note(1, note['id']) == note
        assert bot.ai_state.conversation(1, conversation)['folder_id'] == folder['id']
        assert not store.folder(1, folder['id'])['removed']
        with store.transaction():
            store.db.execute('DROP TRIGGER stop_folder_removal')
        store.delete_folder(1, folder['id'])
        trash = store.trash_folder(1)
        assert store.notes(1, trash['id'])[0]['id'] == note['id']
        destination = store.create_folder(1, 'Восстановлено')
        await bot.notes.move(1, note['id'], destination['id'])
        bot.flush_ai_archive()
        saved = store.note(1, note['id'])
        assert saved['folder_id'] == destination['id'] and saved['source_folder'] == folder['id']
        assert saved['transcript'] == 'Не потерять'
        assert store.notes(1, trash['id']) == []
        assert store.all('PRAGMA foreign_key_check') == []
    asyncio.run(run())



def test_saved_legacy_delete_dialog_never_restores_confirmation_keyboard(tmp_path, store):
    from support.menus import labels

    async def run():
        bot = make_bot(tmp_path, store)
        folder = store.current_folder(1)
        await bot.send_bottom(1, None, {
            'screen': 'dropfolder:' + folder, 'back': 'foldermenu:' + folder,
            'text': 'Удалить папку?', 'rows': [[{'text': 'Удалить', 'callback_data': 'confirmdrop:old'},
                                               {'text': 'Отмена', 'callback_data': 'canceldrop:old'}]],
            'mirror': True,
        })
        assert 'Удалить' not in labels(bot) and 'Отмена' not in labels(bot)
        assert '🗑 Корзина' in labels(bot) and bot.bottom_state(1)['screen'] == 'root'
        assert not store.folder(1, folder)['removed']
    asyncio.run(run())


def test_last_folder_rechecked_after_confirmation_and_rename_survives_restart(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        first = store.ensure_user(1)
        second = store.create_folder(1, 'Вторая')['id']
        await bot.callback(1, None, 500, f'dropfolder:{first}')
        token = bot.folder_delete_confirmations[1][0]
        store.delete_folder(1, second)
        with pytest.raises(ValueError, match='Последнюю'):
            await bot.callback(1, None, 500, f'confirmdrop:{token}')
        with pytest.raises(ValueError, match='Последнюю'):
            await bot.callback(1, None, 500, f'dropfolder:{first}')
        store.rename_folder(1, first, 'Мои записи')
        store.recover_runtime()
        assert store.ensure_user(1) == first
        assert [f['name'] for f in store.folders(1)] == ['Мои записи', 'Корзина']
    asyncio.run(run())


def test_existing_user_folders_are_preserved_on_registration(store):
    identity = store.ensure_user(1)
    store.rename_folder(1, identity, 'Входящие')
    store.create_folder(1, 'Работа')
    before = store.folders(1)
    store.register_user(1)
    store.recover_runtime()
    assert store.folders(1) == before
