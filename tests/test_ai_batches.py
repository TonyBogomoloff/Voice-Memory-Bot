import asyncio

import pytest
from test_ai import bot as bot
from test_ai import ready_note, receive, step, tool
from test_ai_note_actions import request_for

from voice_memory_bot.ai_store import AIStore


async def chain(bot, turns=14):
    # Separate client/server IDs, as in Telegram's shared message sequence.
    source = await receive(bot, 'Исходная идея', message_id=10000)
    unrelated = ready_note(bot, 'Не относится к обсуждению', message_id=20000)
    reply = source['transcript_message_id'] or source['message_id']
    expected = {source['id']}
    conversation = None
    for i in range(turns):
        request = await receive(bot, f'Обсудим шаг {i}', message_id=10001 + i, reply=reply, voice=i == 0)
        bot.assistant.responses = [step(message=f'Ответ {i}')]
        await bot.process_ai(bot.ai_state.claim())
        saved = bot.ai_state.request(1, request['id'])
        payload = bot.assistant.calls[-1][0]
        assert payload['reply_message_id'] == reply
        assert payload['reply_note_id'] in expected
        if conversation:
            assert saved['conversation_id'] == conversation
        conversation = saved['conversation_id']
        expected.update((request['id'], saved['result_note_id']))
        ref = bot.ai_state.message_ref(1, saved['result_note_id'])
        reply = ref['telegram_messages'][0]['message_id']
    return source, unrelated, expected, reply, conversation


@pytest.mark.parametrize('action', ['move_notes', 'delete_notes'])
def test_whole_reply_chain_includes_every_user_and_ai_message(bot, action):
    async def run():
        source, unrelated, expected, reply, conversation = await chain(bot)
        folder = bot.store.create_folder(1, 'Проект')
        request = await receive(bot, 'Перенеси всё обсуждение', message_id=15000, reply=reply)
        payload = bot.ai_engine.payload(bot.ai_state.request(1, request['id']), [])
        assert {n['id'] for n in payload['conversation_messages']} == expected
        assert len(payload['history']) == 14
        assert all(h['user_note_id'] and h['assistant_note_id'] and h['assistant_message']['telegram_messages']
                   for h in payload['history'])
        ids_before = {m['message_id'] for n in payload['conversation_messages'] for m in n['telegram_messages']}
        bot.assistant.responses = [step('list_conversation'), step(action, note_ids=[*expected, source['id']],
            folder_id=folder['id'] if action == 'move_notes' else ''), step(message='Готово')]
        await bot.process_ai(bot.ai_state.claim())
        current = bot.ai_state.request(1, request['id'])
        assert current['state'] == 'ready' and current['result_note_id'] is None
        jobs = bot.store.all('SELECT * FROM ai_message_jobs WHERE request_id=?', (request['id'],))
        assert len(jobs) == len(expected) == 29 and all(j['state'] == 'done' for j in jobs)
        if action == 'move_notes':
            assert {n['id'] for n in bot.store.notes(1, folder['id'], limit=-1)} == expected
            assert {n['id'] for n in bot.ai_state.conversation_notes(1, conversation, exclude=request['id'])} == expected
            # Re-rendered answer must still lead to the same conversation after moving.
            answer = next(n for n in bot.store.notes(1, folder['id']) if n['ai_generated_from'])
            await bot.show_note(1, answer['id'], None)
            message_id = bot.telegram.message_sequence
            assert bot.ai_state.reply_conversation(1, message_id) == conversation
        else:
            assert all(bot.store.one('SELECT deleted FROM notes WHERE id=?', (identity,))['deleted'] for identity in expected)
        deleted_ids = {p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage'}
        assert ids_before <= deleted_ids
        assert bot.store.note(1, unrelated['id'])['transcript'] == unrelated['transcript']
        before = len(bot.telegram.sent)
        await bot.ai_note_actions.execute(1, request['id'], 1)
        assert len(bot.telegram.sent) == before
        AIStore(bot.store).archive_replies()
        assert bot.ai_state.request(1, request['id'])['result_note_id'] is None
        assert not bot.store.all('PRAGMA foreign_key_check')
    asyncio.run(run())


def test_batch_storage_failure_rolls_back_every_item(bot, monkeypatch):
    async def run():
        notes = [ready_note(bot, message_id=10000 + i) for i in range(3)]
        request = request_for(bot)
        folder = bot.store.create_folder(1, 'Работа')
        tool(bot, request, step('search_notes'))
        bot.ai_state.save_command(1, request, 1, step('move_notes', note_ids=[n['id'] for n in notes], folder_id=folder['id']))
        apply = bot.ai_note_actions.apply
        calls = 0
        def failing(*args):
            nonlocal calls
            apply(*args)
            calls += 1
            if calls == 2:
                raise OSError('storage interruption')
        monkeypatch.setattr(bot.ai_note_actions, 'apply', failing)
        with pytest.raises(OSError):
            await bot.ai_note_actions.execute(1, request, 1)
        assert all(bot.store.note(1, n['id'])['folder_id'] == n['folder_id'] for n in notes)
        assert not bot.store.all('SELECT * FROM ai_message_jobs')
        assert bot.ai_state.steps(1, request)[1]['result'] is None
    asyncio.run(run())


@pytest.mark.parametrize('target', ['new', 'existing', 'cancel'])
def test_one_folder_choice_controls_entire_batch(bot, target):
    async def run():
        _, _, expected, reply, _ = await chain(bot, 2)
        folder = bot.store.create_folder(1, 'Проекты')
        request = await receive(bot, 'Перенеси обсуждение в Проект', message_id=15000, reply=reply)
        bot.assistant.responses = [step('list_conversation'), step('move_notes', note_ids=list(expected), title='Проект')]
        await bot.process_ai(bot.ai_state.claim())
        assert len(bot.ai_note_actions.choices(1, request['id'])) == 1
        before = {identity: bot.store.note(1, identity)['folder_id'] for identity in expected}
        await bot.ai_note_actions.choose(1, request['id'], 1, folder['id'] if target == 'existing' else target)
        after = {identity: bot.store.note(1, identity)['folder_id'] for identity in expected}
        if target == 'cancel':
            assert before == after
        else:
            assert len(set(after.values())) == 1
            assert bot.store.folder(1, next(iter(after.values())))['name'] == ('Проект' if target == 'new' else 'Проекты')
        await bot.ai_note_actions.choose(1, request['id'], 1, 'new')
        AIStore(bot.store).archive_replies()
        assert bot.ai_state.request(1, request['id'])['result_note_id'] is None
    asyncio.run(run())


@pytest.mark.parametrize('invalid', ['stale', 'foreign', 'busy', 'access', 'self'])
def test_batch_prevalidates_all_targets_before_any_mutation(bot, invalid):
    async def run():
        notes = [ready_note(bot, message_id=10000 + i) for i in range(3)]
        request = request_for(bot)
        folder = bot.store.create_folder(1, 'Работа')
        tool(bot, request, step('search_notes'))
        ids = [n['id'] for n in notes]
        if invalid == 'stale':
            bot.store.update_note(1, ids[-1], title='Изменено')
        elif invalid == 'foreign':
            ids.append(ready_note(bot, owner=2, message_id=20000)['id'])
        elif invalid == 'busy':
            await bot.note_locks[ids[-1]].acquire()
        elif invalid == 'access':
            bot.ai_state.set_settings(1, notes_access='read')
        else:
            ids.append(request)
        bot.ai_state.save_command(1, request, 1, step('move_notes', note_ids=ids, folder_id=folder['id']))
        result = await bot.ai_note_actions.execute(1, request, 1)
        assert 'error' in result
        assert all(bot.store.note(1, n['id'])['folder_id'] == n['folder_id'] for n in notes)
        assert not bot.store.all('SELECT * FROM ai_message_jobs')
        if invalid == 'busy':
            bot.note_locks[ids[-1]].release()
    asyncio.run(run())


def test_batch_cleanup_resumes_after_interruption_without_repeating_mutations(bot, monkeypatch):
    async def run():
        notes = [ready_note(bot, message_id=10000 + i) for i in range(20)]
        for note in notes:
            bot.messages.record(note, note['message_id'], None, 'source')
        request = request_for(bot)
        folder = bot.store.create_folder(1, 'Проект')
        tool(bot, request, step('search_notes'))
        bot.ai_state.save_command(1, request, 1, step('move_notes', note_ids=[n['id'] for n in notes], folder_id=folder['id']))
        original = bot.telegram.call
        calls = 0
        async def interrupted(method, **payload):
            nonlocal calls
            if method == 'deleteMessage':
                calls += 1
                if calls == 3:
                    raise asyncio.CancelledError
            return await original(method, **payload)
        monkeypatch.setattr(bot.telegram, 'call', interrupted)
        with pytest.raises(asyncio.CancelledError):
            await bot.ai_note_actions.execute(1, request, 1)
        assert all(bot.store.note(1, n['id'])['revision'] == 1 for n in notes)
        monkeypatch.setattr(bot.telegram, 'call', original)
        bot.init_ai(bot.assistant)
        result = await bot.ai_note_actions.execute(1, request, 1)
        assert len(result['items']) == 20 and all(r['cleanup_state'] == 'done' for r in result['items'])
        assert all(bot.store.note(1, n['id'])['revision'] == 1 for n in notes)
        deleted = [p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage']
        assert len(deleted) == len(set(deleted)) == 20
        # All added foreign-key tables must participate in account cleanup.
        bot.cleanup.begin(1)
        assert not bot.store.all('PRAGMA foreign_key_check')
    asyncio.run(run())
