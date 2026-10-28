import asyncio
import sqlite3
import time

import pytest
from support.base import make_bot
from support.commands import receive
from support.menus import reply_keyboard

from voice_memory_bot.config import Limits
from voice_memory_bot.custom_emoji import icon_id, themed_markup
from voice_memory_bot.providers import ProviderError
from voice_memory_bot.store import Store
from voice_memory_bot.trash import TRASH_DESCRIPTION


def ready(store, folder, message, owner=1):
    note, _ = store.add_note(owner, folder, {'message_id': message}, 'text', transcript=f'Запись {message}')
    store.update_note(owner, note['id'], state='ready', delivery='sent', transcript_delivery='sent')
    return store.note(owner, note['id'])


@pytest.mark.parametrize(('count', 'page_label'), [(0, '1 из 1'), (11, '2 из 2')])
def test_trash_icon_description_and_top_right_danger_button(tmp_path, store, count, page_label):
    async def run():
        bot = make_bot(tmp_path, store)
        trash = store.trash_folder(1)['id']
        store.set_folder_page_size(1, 10)
        for i in range(count):
            ready(store, trash, 1000 + i)
        await bot.show_notes(1, None, trash, page=0)
        header = next(p for m, p in bot.telegram.sent if m == 'sendMessage'
                      and 'страница ' in p.get('text', ''))
        assert header['text'].startswith(f'🗑 Корзина (страница {page_label})')
        assert TRASH_DESCRIPTION in header['text']
        assert 'Это системная папка' not in header['text']
        keys = reply_keyboard(bot)
        trash_button = next(b for row in keys['keyboard'] for b in row if b['text'] == '🗑 Корзина')
        assert trash_button['style'] == 'primary'
        themed, aliases = themed_markup(keys)
        actual = next(b for row in themed['keyboard'] for b in row if b['text'] == 'Корзина')
        assert actual['icon_custom_emoji_id'] == icon_id('delete')
        assert aliases['Корзина'] == '🗑 Корзина'
        await receive(bot, '📁 Папка', 2000)
        assert reply_keyboard(bot)['keyboard'][0][1] == {'text': '🗑 Очистить корзину', 'style': 'danger'}
        assert bot.bottom_state(1)['screen'] == 'foldermenu:' + trash
    asyncio.run(run())


@pytest.mark.parametrize('busy', ['export', 'note_lock', 'ai_lock', 'processing', 'delivery', 'transcript', 'ai'])
def test_confirmation_keeps_keyboard_and_checks_owner_prompt_expiry_cancel_and_busy(tmp_path, store, busy):
    async def run():
        bot = make_bot(tmp_path, store)
        trash = store.trash_folder(1)['id']
        note = ready(store, trash, 1000)
        await bot.show_notes_menu(1, None, trash)
        before, state = reply_keyboard(bot), bot.bottom_state(1)
        sent = len(bot.telegram.sent)
        await receive(bot, '🗑 Очистить корзину', 2000)
        confirmation = bot.trash_confirmations[1]
        prompt = confirmation['prompt']
        action = 'confirmtrash:' + confirmation['token']
        new_keyboards = [p for m, p in bot.telegram.sent[sent:] if m == 'sendMessage' and p.get('keyboard')]
        assert len(new_keyboards) == 1
        assert new_keyboards[0]['keyboard']['inline_keyboard'][0] == [
            {'text': 'Очистить корзину', 'callback_data': action, 'style': 'danger'},
            {'text': 'Отмена', 'callback_data': 'canceltrash:' + confirmation['token']}]
        assert reply_keyboard(bot) == before and bot.bottom_state(1) == state
        for owner, mid in ((2, prompt), (1, prompt + 1), (1, None)):
            with pytest.raises(ValueError, match='устарело'):
                await bot.callback(owner, None, mid, action)
        lock = None
        if busy in {'export', 'note_lock', 'ai_lock'}:
            lock = {'export': bot.export_locks[1], 'note_lock': bot.note_locks[note['id']],
                    'ai_lock': bot.ai_delivery_locks[note['id']]}[busy]
            await lock.acquire()
        elif busy == 'ai':
            bot.ai_state.enqueue(1, note['id'], 'button')
            bot.ai_state.update_request(1, note['id'], state='running')
            note = store.note(1, note['id'])
        else:
            field, value = {'processing': ('state', 'processing'), 'delivery': ('delivery', 'sending'),
                            'transcript': ('transcript_delivery', 'sending')}[busy]
            store.update_note(1, note['id'], **{field: value})
        with pytest.raises(ValueError, match='завершения'):
            await bot.callback(1, None, prompt, action)
        if lock:
            lock.release()
        elif busy == 'ai':
            bot.ai_state.update_request(1, note['id'], state='deleted', delivery='sent')
        else:
            store.update_note(1, note['id'], **{field: note[field]})
        assert store.note(1, note['id']) == note
        await bot.callback(1, None, prompt, 'canceltrash:' + confirmation['token'])
        assert reply_keyboard(bot) == before and bot.bottom_state(1) == state
        with pytest.raises(ValueError, match='устарело'):
            await bot.callback(1, None, prompt, action)
        await bot.callback(1, None, None, 'cleartrash:' + trash)
        confirmation = bot.trash_confirmations[1]
        confirmation['expires'] = time.monotonic() - 1
        with pytest.raises(ValueError, match='устарело'):
            await bot.callback(1, None, confirmation['prompt'], 'confirmtrash:' + confirmation['token'])
        assert store.note(1, note['id']) == note
    asyncio.run(run())


def test_clear_only_confirmed_notes_still_in_owners_trash_and_their_exact_messages(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        trash = store.trash_folder(1)['id']
        inbox = store.ensure_user(1)
        target = ready(store, trash, 1000)
        restored = ready(store, trash, 1001)
        outside = ready(store, inbox, 1002)
        other = ready(store, store.trash_folder(2)['id'], 1000, owner=2)
        for note in (target, restored, outside, other):
            bot.archive.persist_text(note, store.folder(note['owner'], note['folder_id']))
            bot.messages.record(note, note['message_id'] + 100, None, 'view')
            bot.ai_state._snapshot(note)
        bot.ai_state.start_note_conversation(1, target['id'])
        request = bot.ai_state.enqueue(1, target['id'], 'button')
        bot.ai_state.update_request(1, target['id'], state='ready', delivery='sent')
        target = store.note(1, target['id'])
        with store.transaction():
            store.db.execute('INSERT INTO ai_progress VALUES (?,?)', (target['id'], 1200))
            store.db.execute('INSERT INTO ai_steps VALUES (?,0,?,?)', (target['id'], '{}', '{}'))
            store.db.execute("INSERT INTO ai_message_jobs VALUES (?,0,?,?, 'delete','done',0)",
                             (target['id'], 1, outside['id']))
            store.db.execute('INSERT INTO ai_active VALUES (?,?,?) ON CONFLICT(owner,folder_id) DO NOTHING',
                             (1, trash, request['conversation_id']))
        # One shared AI conversation contains a surviving request and its answer
        # in the trash. Clearing the answer must not erase that outside request.
        bot.ai_state.enqueue(1, outside['id'], 'button', conversation=request['conversation_id'])
        bot.ai_state.finish_reply(1, outside['id'], 'Ответ, который уйдёт в корзину')
        bot.ai_state.update_request(1, outside['id'], delivery='sent')
        answer_id = bot.ai_state.request(1, outside['id'])['result_note_id']
        store.update_note(1, answer_id, folder_id=trash, delivery='sent', transcript_delivery='sent')
        answer = store.note(1, answer_id)
        bot.messages.record(answer, 1300, None, 'view')
        bot.archive.persist_text(answer, store.folder(1, trash))
        # A stale legacy field on a trashed note must never delete a surviving note.
        store.update_note(1, target['id'], result_message_id=1102)
        bot.ai_state.link(1, 1102, request['conversation_id'])
        await bot.show_notes_menu(1, None, trash)
        await bot.callback(1, None, None, 'cleartrash:' + trash)
        confirmation = bot.trash_confirmations[1]
        await bot.notes.move(1, restored['id'], inbox)
        late = ready(store, trash, 1003)
        survivors = [store.note(1, n['id']) for n in (restored, outside, late)] + [other]
        bot.cleanup.remember(1, 9999)  # Unrelated chat history, never a cleanup target.
        before_keyboard, before_state = reply_keyboard(bot), bot.bottom_state(1)
        bot.telegram.sent.clear()
        await bot.callback(1, None, confirmation['prompt'], 'confirmtrash:' + confirmation['token'])
        assert not store.one('SELECT 1 FROM notes WHERE id IN (?,?)', (target['id'], answer_id))
        for note in survivors:
            assert store.note(note['owner'], note['id']) == note
        for note in (restored, outside, other):
            assert bot.archive.directory(note).exists()
        assert not bot.archive.directory(target).exists() and not bot.archive.directory(answer).exists()
        deleted = {mid for method, p in bot.telegram.sent for mid in (
            p.get('message_ids', []) if method == 'deleteMessages' else
            [p['message_id']] if method == 'deleteMessage' else [])}
        assert deleted == {1000, 1100, 1200, 1300}
        assert all(p['chat_id'] == 1 for m, p in bot.telegram.sent if m.startswith('delete'))
        assert store.one('SELECT 1 FROM telegram_messages WHERE owner=1 AND message_id=9999')
        assert store.one('SELECT 1 FROM ai_links WHERE owner=1 AND message_id=1102')
        assert store.one('SELECT 1 FROM ai_requests WHERE note_id=?', (outside['id'],))
        assert bot.ai_state.request(1, outside['id'])['response'] is None
        bot.ai_state.archive_replies()
        assert not store.one('SELECT 1 FROM notes WHERE id=?', (answer_id,))
        assert store.notes(1, trash) == [late]
        assert bot.bottom_state(1) == before_state
        assert not any(m == 'sendMessage' and p.get('keyboard', {}).get('keyboard')
                       for m, p in bot.telegram.sent)
        assert before_keyboard['keyboard'][0][1]['style'] == 'danger'
        assert store.all('PRAGMA foreign_key_check') == []
        assert store.trash_folder(1)['id'] == trash
    asyncio.run(run())


def test_atomic_scope_validation_and_rollback(tmp_path, store):
    bot = make_bot(tmp_path, store)
    trash = store.trash_folder(1)['id']
    target = ready(store, trash, 1000)
    bot.messages.record(target, 1100, None, 'view')
    with pytest.raises(ValueError):
        bot.trash.begin(2, trash, [target['id']])
    with pytest.raises(ValueError):
        bot.trash.begin(1, store.ensure_user(1), [target['id']])
    with store.transaction():
        store.db.execute("CREATE TRIGGER stop_purge BEFORE DELETE ON notes BEGIN SELECT RAISE(ABORT, 'disk error'); END")
    with pytest.raises(sqlite3.IntegrityError, match='disk error'):
        bot.trash.begin(1, trash, [target['id']])
    assert store.note(1, target['id']) == target
    assert bot.trash.jobs(1) == []
    assert store.one('SELECT 1 FROM note_messages WHERE note_id=?', (target['id'],))
    assert store.all('PRAGMA foreign_key_check') == []


def test_interrupted_cleanup_resumes_exact_snapshot_after_restart(tmp_path, monkeypatch):
    async def run():
        path = tmp_path / 'db'
        store = Store(path, Limits())
        trash = store.trash_folder(1)['id']
        bot = make_bot(tmp_path, store)
        note = ready(store, trash, 1000)
        bot.archive.persist_text(note, store.folder(1, trash))
        bot.trash.begin(1, trash, [note['id']])
        async def unavailable(*args, **kwargs):
            raise ProviderError('telegram_transport_unknown')
        def locked_file(*args):
            raise OSError('file busy')
        monkeypatch.setattr(bot.telegram, 'call', unavailable)
        monkeypatch.setattr(bot.archive, 'delete', locked_file)
        await bot.resume_trash_cleanup(1)
        assert bot.trash.pending(1) and bot.archive.directory(note).exists()
        store.close()
        store = Store(path, Limits())
        try:
            bot = make_bot(tmp_path, store)
            new = ready(store, trash, 1001)
            bot.archive.persist_text(new, store.folder(1, trash))
            await bot.resume_trash_cleanup(1)
            assert bot.trash.jobs(1) == [] and not bot.archive.directory(note).exists()
            assert store.note(1, new['id']) == new and bot.archive.directory(new).exists()
            assert bot.telegram.sent == [('deleteMessages', {'chat_id': 1, 'message_ids': [1000]})]
        finally:
            store.close()
    asyncio.run(run())


def test_forbidden_telegram_deletion_reports_partial_without_retrying_other_messages(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        trash = store.trash_folder(1)['id']
        note = ready(store, trash, 1000)
        await bot.callback(1, None, None, 'cleartrash:' + trash)
        confirmation = bot.trash_confirmations[1]
        original = bot.telegram.call
        async def forbidden(method, **kwargs):
            if method in {'deleteMessage', 'deleteMessages'}:
                error = ProviderError('telegram_400')
                error.reason = 'delete_forbidden'
                raise error
            return await original(method, **kwargs)
        bot.telegram.call = forbidden
        await bot.callback(1, None, confirmation['prompt'], 'confirmtrash:' + confirmation['token'])
        assert not store.one('SELECT 1 FROM notes WHERE id=?', (note['id'],))
        assert not bot.trash.pending(1) and bot.trash.jobs(1)[0]['blocked_ids'] == '[1000]'
        assert 'не разрешил удалить' in bot.telegram.sent[-1][1]['text']
    asyncio.run(run())


def test_large_cleanup_checkpoints_batches_and_retry_never_includes_new_messages(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        trash = store.trash_folder(1)['id']
        note = ready(store, trash, 1000)
        for mid in range(1001, 1202):
            bot.messages.record(note, mid, None, 'view')
        bot.trash.begin(1, trash, [note['id']])
        original = bot.telegram.call
        async def fail_second_batch(method, **kwargs):
            if method == 'deleteMessages' and 1100 in kwargs['message_ids']:
                raise ProviderError('telegram_transport_unknown')
            return await original(method, **kwargs)
        bot.telegram.call = fail_second_batch
        await bot.resume_trash_cleanup(1)
        assert bot.trash.pending(1)
        assert bot.trash.jobs(1)[0]['message_ids'].startswith('[1100,')
        bot = make_bot(tmp_path, store)
        fresh = ready(store, trash, 2000)
        await bot.resume_trash_cleanup(1)
        assert store.note(1, fresh['id']) == fresh and bot.trash.jobs(1) == []
        batches = [p['message_ids'] for m, p in bot.telegram.sent if m == 'deleteMessages']
        assert [len(batch) for batch in batches] == [100, 2]
        assert {mid for batch in batches for mid in batch} == set(range(1100, 1202))
    asyncio.run(run())


def test_trash_opens_full_recent_page_and_shows_deleted_folder_records(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        folder = store.create_folder(1, 'Удаляемая')['id']
        for i in range(20):
            note = ready(store, folder, 1000 + i)
            store.db.execute('UPDATE notes SET entry_type=?,created_at=? WHERE id=?',
                             ('ai' if i % 2 else 'note', i, note['id']))
        store.db.commit()
        store.delete_folder(1, folder)
        trash = store.trash_folder(1)['id']
        ready(store, trash, 2000)
        assert len(bot.trash.snapshot(1, trash)) == 21
        await bot.show_notes(1, None, trash)
        header = next(p for m, p in bot.telegram.sent if m == 'sendMessage')
        assert header['text'].startswith('🗑 Корзина (страница 2 из 2)')
        assert header['keyboard']['inline_keyboard'] == [[{'text': '⬅️', 'callback_data': f'folder:{trash}:1'}]]
        first = [p['text'] for m, p in bot.telegram.sent if m == 'sendMessage'
                 and p['text'].startswith('Запись ')]
        assert len(first) == 20 and 'Запись 2000' in first
        assert store.one('SELECT page FROM folder_views WHERE owner=1')['page'] == 0
        bot.telegram.sent.clear()
        await receive(bot, '🗑 Корзина', 3000)
        assert not any(m == 'sendMessage' for m, _ in bot.telegram.sent)
        await bot.callback(1, None, None, f'folder:{trash}:1')
        header = next(p for m, p in bot.telegram.sent if m == 'sendMessage')
        assert header['text'].startswith('🗑 Корзина (страница 1 из 2)')
        assert header['keyboard']['inline_keyboard'] == [[{'text': '➡️', 'callback_data': f'folder:{trash}:0'}]]
        second = [p['text'] for m, p in bot.telegram.sent if m == 'sendMessage'
                  and p['text'].startswith('Запись ')]
        assert second == ['Запись 1000']
        assert set(first + second) == {f'Запись {1000 + i}' for i in range(20)} | {'Запись 2000'}
        bot.telegram.sent.clear()
        await receive(bot, '🗑 Корзина', 3001)
        assert store.one('SELECT page FROM folder_views WHERE owner=1')['page'] == 0
        assert len([p for m, p in bot.telegram.sent if m == 'sendMessage'
                    and p['text'].startswith('Запись ')]) == 20
    asyncio.run(run())
