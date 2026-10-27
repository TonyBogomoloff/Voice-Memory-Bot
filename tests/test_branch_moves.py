import asyncio

import pytest
from support.ai import bot as bot
from support.ai import ready_note, receive, step
from support.base import make_bot

from voice_memory_bot.config import Limits
from voice_memory_bot.store import Store


async def shopping_branch(bot):
    source = await receive(bot, message_id=10000, voice=True)
    members = [source]
    for index, text in enumerate(('Оформи список покупок', 'Добавь количество', 'Сгруппируй по отделам')):
        selected = members[-1]
        reply = bot.ai_state.message_ref(1, selected['id'])['telegram_messages'][0]['message_id']
        if index != 1:
            # A fresh /ai operation on a saved answer creates another conversation.
            await bot.callback(1, None, reply, f"aiask:{selected['id']}")
        request = await receive(bot, text, message_id=10001 + index, reply=reply if index == 1 else None)
        bot.assistant.responses = [step(message=f'Список покупок, версия {index + 1}')]
        await bot.process_ai(bot.ai_state.claim())
        result = bot.ai_state.request(1, request['id'])['result_note_id']
        members.extend((request, bot.store.note(1, result)))
    unrelated = await receive(bot, 'Независимая заметка в той же папке', message_id=11000)
    return members, unrelated


def ids(notes):
    return {n['id'] for n in notes}


@pytest.mark.parametrize('selected,new_folder', [(0, False), (2, True), (-1, False)])
def test_move_any_message_moves_whole_branch_and_preserves_archive(bot, selected, new_folder):
    async def run():
        members, unrelated = await shopping_branch(bot)
        foreign = ready_note(bot, owner=2, message_id=members[0]['message_id'])
        original = bot.archive.audio(members[0]).read_bytes()
        target = bot.store.create_folder(1, 'Покупки')
        note = members[selected]
        reply = bot.ai_state.message_ref(1, note['id'])['telegram_messages'][0]['message_id']
        source_messages = {r['message_id'] for n in members for r in bot.messages.receipts(1, n['id'])
                           if not r['deleted']}
        bot.telegram.sent.clear()
        await bot.handle({'message': {'message_id': 12000, 'from': {'id': 1},
            'chat': {'id': 1, 'type': 'private'}, 'text': '/move', 'reply_to_message': {'message_id': reply}}})
        session = bot.store.one('SELECT * FROM reply_moves WHERE owner=1')
        prompt = next(p for m, p in reversed(bot.telegram.sent) if m == 'sendMessage')
        assert 'всю ветку' in prompt['text'] and '7' in prompt['text']
        if new_folder:
            await bot.reply_move_new_folder(1, None, session['token'], 'Новые покупки')
            target = next(f for f in bot.store.folders(1) if f['name'] == 'Новые покупки')
        else:
            await bot.callback(1, None, session['prompt_id'], f"rmove:{session['token']}:{target['id']}")
        bot.flush_ai_archive()
        assert ids(bot.store.notes(1, target['id'], limit=-1)) == ids(members)
        assert ids(bot.store.export_notes(1, target['id'])) == ids(members)
        assert bot.archive.audio(members[0]).read_bytes() == original
        assert bot.store.note(1, unrelated['id'])['folder_id'] == unrelated['folder_id']
        assert bot.store.note(2, foreign['id'])['folder_id'] == foreign['folder_id']
        assert bot.store.current_folder(1) == unrelated['folder_id']
        deleted = {p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage'}
        assert source_messages <= deleted and unrelated['message_id'] not in deleted
        assert 12000 not in deleted
        assert all(bot.store.note(1, n['id'])['revision'] == n['revision'] + 1 for n in members)
        assert {c['folder_id'] for c in bot.store.all('SELECT * FROM ai_conversations WHERE owner=1')} == {target['id']}
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['locked', 'queued_ai', 'missing_media', 'storage'])
def test_entire_branch_is_validated_and_committed_before_cleanup(bot, monkeypatch, failure):
    async def run():
        members, _ = await shopping_branch(bot)
        target = bot.store.create_folder(1, 'Покупки')
        if failure == 'locked':
            await bot.note_locks[members[-1]['id']].acquire()
        elif failure == 'queued_ai':
            bot.ai_state.update_request(1, members[1]['id'], state='queued')
        elif failure == 'missing_media':
            bot.store.update_note(1, members[0]['id'], audio_bytes=0)
        else:
            move = bot.messages.move_archive
            calls = 0

            def fail(*args):
                nonlocal calls
                result = move(*args)
                calls += 1
                if calls == 2:
                    raise OSError('disk failure')
                return result

            monkeypatch.setattr(bot.messages, 'move_archive', fail)
        bot.telegram.sent.clear()
        try:
            with pytest.raises(OSError if failure == 'storage' else ValueError):
                await bot.move(1, members[2]['id'], target['id'], None)
        finally:
            if failure == 'locked':
                bot.note_locks[members[-1]['id']].release()
        assert all(bot.store.note(1, n['id'])['folder_id'] == n['folder_id'] for n in members)
        assert not bot.store.all('SELECT * FROM note_moves')
        assert not any(m == 'deleteMessage' for m, _ in bot.telegram.sent)
    asyncio.run(run())


def test_branch_cleanup_resumes_after_restart_and_branch_can_move_back(bot, tmp_path, monkeypatch):
    async def run():
        members, unrelated = await shopping_branch(bot)
        target = bot.store.create_folder(1, 'Покупки')
        call = bot.telegram.call
        deleted = []

        async def interrupt(method, **payload):
            if method == 'deleteMessage':
                if deleted:
                    raise asyncio.CancelledError
                deleted.append(payload['message_id'])
            return await call(method, **payload)

        monkeypatch.setattr(bot.telegram, 'call', interrupt)
        with pytest.raises(asyncio.CancelledError):
            await bot.move(1, members[-1]['id'], target['id'], None)
        assert ids(bot.store.notes(1, target['id'], limit=-1)) == ids(members)
        bot.store.close()
        store = Store(tmp_path / 'db', Limits())
        try:
            restarted = make_bot(tmp_path, store)
            restarted.messages.record(store.note(1, members[-1]['id']), 99000, None, 'view')
            while pending := restarted.messages.pending():
                await restarted.cleanup_move(pending)
            resumed = {p['message_id'] for m, p in restarted.telegram.sent if m == 'deleteMessage'}
            assert not set(deleted) & resumed and 99000 not in resumed
            assert ids(restarted.notes.branch(1, [members[2]['id']]).notes) == ids(members)
            await restarted.move(1, members[2]['id'], members[0]['folder_id'], None)
            assert not store.notes(1, target['id'])
            assert all(store.note(1, n['id'])['revision'] == n['revision'] + 2 for n in members)
            assert store.note(1, unrelated['id'])['folder_id'] == unrelated['folder_id']
        finally:
            store.close()
    asyncio.run(run())


@pytest.mark.parametrize('choose_folder', [False, True])
def test_single_ai_move_expands_branch_and_has_durable_per_message_receipts(bot, choose_folder):
    async def run():
        members, unrelated = await shopping_branch(bot)
        target = bot.store.create_folder(1, 'Покупки')
        selected = members[-1]
        reply = bot.ai_state.message_ref(1, selected['id'])['telegram_messages'][0]['message_id']
        request = await receive(bot, 'Перенеси в покупки', message_id=12000, reply=reply)
        bot.assistant.responses = [step('read_note', note_id=selected['id']),
            step('move_note', note_id=selected['id'], revision=selected['revision'],
                 folder_id='' if choose_folder else target['id'], title='Покупочки' if choose_folder else ''),
            step(message='Готово')]
        await bot.process_ai(bot.ai_state.claim())
        if choose_folder:
            assert not bot.store.notes(1, target['id'])
            await bot.ai_note_actions.choose(1, request['id'], 1, target['id'])
        assert ids(bot.store.notes(1, target['id'], limit=-1)) == ids(members)
        jobs = bot.store.all('SELECT * FROM ai_message_jobs WHERE request_id=?', (request['id'],))
        assert {j['note_id'] for j in jobs} == ids(members) and all(j['state'] == 'done' for j in jobs)
        result = await bot.ai_note_actions.execute(1, request['id'], 1)
        assert {r['id'] for r in result['items']} == ids(members)
        assert all(bot.store.note(1, n['id'])['revision'] == n['revision'] + 1 for n in members)
        assert bot.store.note(1, request['id'])['folder_id'] == request['folder_id']
        assert bot.store.note(1, unrelated['id'])['folder_id'] == unrelated['folder_id']
        assert bot.ai_state.request(1, request['id'])['result_note_id'] is None
    asyncio.run(run())


def test_moved_active_chat_does_not_capture_new_independent_messages(bot):
    async def run():
        folder = bot.store.current_folder(1)
        bot.ai_state.mode(1, folder, 'chat')
        request = await receive(bot, 'Составь список', message_id=10000)
        await bot.process_ai(bot.ai_state.claim())
        old = bot.ai_state.request(1, request['id'])
        target = bot.store.create_folder(1, 'Покупки')
        await bot.move(1, old['result_note_id'], target['id'], None)
        new = await receive(bot, 'Другая тема', message_id=10001)
        assert bot.ai_state.request(1, new['id'])['conversation_id'] != old['conversation_id']
        assert new['id'] not in ids(bot.notes.branch(1, [request['id']]).notes)
    asyncio.run(run())


def test_deleted_intermediate_and_old_receipts_keep_surviving_branch_connected(bot):
    async def run():
        members, unrelated = await shopping_branch(bot)
        middle = members[2]
        bot.store.delete(1, middle['id'])
        target = bot.store.create_folder(1, 'Покупки')
        await bot.move(1, members[-1]['id'], target['id'], None)
        assert ids(bot.store.notes(1, target['id'], limit=-1)) == ids(members) - {middle['id']}
        assert bot.store.one('SELECT deleted FROM notes WHERE id=?', (middle['id'],))['deleted']
        assert bot.store.note(1, unrelated['id'])['folder_id'] == unrelated['folder_id']
    asyncio.run(run())


@pytest.mark.parametrize('change', ['revision', 'new_reply'])
def test_ai_folder_choice_rechecks_automatic_branch_members(bot, change):
    async def run():
        members, _ = await shopping_branch(bot)
        selected = members[-1]
        reply = bot.ai_state.message_ref(1, selected['id'])['telegram_messages'][0]['message_id']
        request = await receive(bot, 'Перенеси в покупочки', message_id=12000, reply=reply)
        bot.assistant.responses = [step('read_note', note_id=selected['id']),
            step('move_note', note_id=selected['id'], revision=selected['revision'], title='Покупочки')]
        await bot.process_ai(bot.ai_state.claim())
        if change == 'revision':
            bot.store.update_note(1, members[0]['id'], title='Исправленный список')
        else:
            await receive(bot, 'Ещё уточнение', message_id=12001, reply=reply)
            await bot.process_ai(bot.ai_state.claim())
        folders_before = ids(bot.store.folders(1))
        with pytest.raises(ValueError, match='изменил'):
            await bot.ai_note_actions.choose(1, request['id'], 1, 'new')
        assert ids(bot.store.folders(1)) == folders_before
        assert not bot.store.all('SELECT * FROM note_moves')
        assert all(bot.store.note(1, n['id'])['folder_id'] == n['folder_id'] for n in members)
    asyncio.run(run())
