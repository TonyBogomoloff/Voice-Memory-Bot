import asyncio

import pytest
from support.ai import bot as bot
from support.ai import ready_note, receive, step


async def make_branch(bot):
    source = await receive(bot, message_id=10000, voice=True)
    request = await receive(bot, 'Составь список', message_id=10001, reply=source['transcript_message_id'])
    bot.assistant.responses = [step(message='Первый ответ')]
    await bot.process_ai(bot.ai_state.claim())
    answer = bot.store.note(1, bot.ai_state.request(1, request['id'])['result_note_id'])
    # A new scoped conversation on an answer must still belong to the original branch.
    await bot.callback(1, None, None, f"aiask:{answer['id']}")
    followup = await receive(bot, message_id=10002, voice=True)
    bot.assistant.responses = [step(message='Последний ответ')]
    await bot.process_ai(bot.ai_state.claim())
    last = bot.store.note(1, bot.ai_state.request(1, followup['id'])['result_note_id'])
    unrelated = await receive(bot, 'Независимая заметка', message_id=11000)
    bot.flush_ai_archive()
    return [source, request, answer, followup, last], unrelated


def receipts(bot, notes):
    return {r['message_id'] for note in notes for r in bot.messages.receipts(1, note['id']) if not r['deleted']}


async def confirm_delete(bot, message_id):
    await bot.handle({'message': {'message_id': 12000, 'from': {'id': 1},
        'chat': {'id': 1, 'type': 'private'}, 'text': '/delete',
        'reply_to_message': {'message_id': message_id}}})
    return bot.bottom_state(1)


@pytest.mark.parametrize('selected,transcript', [
    (0, False), (0, True), (1, False), (2, False), (4, False), (3, False), (3, True)])
def test_source_deletes_branch_request_deletes_its_answers_and_answer_deletes_itself(bot, selected, transcript):
    async def run():
        members, unrelated = await make_branch(bot)
        note = bot.store.note(1, members[selected]['id'])
        foreign = ready_note(bot, owner=2, message_id=note['message_id'])
        target = note['transcript_message_id'] if transcript else min(receipts(bot, [note]))
        expected = {0: members, 1: members[1:3], 3: members[3:5]}.get(selected, [note])
        kept = [n for n in members if n['id'] not in {m['id'] for m in expected}] + [unrelated]
        removed_ids, kept_ids = receipts(bot, expected), receipts(bot, kept)
        state = await confirm_delete(bot, target)
        assert ('всю ветку' in state['text']) == (selected == 0)
        if selected in {1, 3}:
            assert state['text'] == 'Удалить запрос вместе с ответом ИИ?'
        assert all(bot.store.note(1, n['id']) for n in members)
        bot.telegram.sent.clear()
        await bot.callback(1, None, state['inline_prompt_id'], state['actions']['Удалить'])
        for deleted in expected:
            assert bot.store.one('SELECT deleted FROM notes WHERE id=?', (deleted['id'],))['deleted']
            assert not bot.archive.directory(deleted).exists()
        assert all(bot.store.note(1, n['id']) for n in kept)
        assert bot.store.note(2, foreign['id'])['transcript'] == foreign['transcript']
        sent_ids = {p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage'}
        assert removed_ids <= sent_ids and not kept_ids & sent_ids
        if selected in {2, 4}:
            request = bot.ai_state.request(1, members[selected - 1]['id'])
            assert request['response'] is None and request['result_note_id'] == note['id']
            assert request['delivery'] == 'cancelled'
            before = len(bot.telegram.sent)
            await bot.deliver_ai(request, force=True)
            assert len(bot.telegram.sent) == before
        bot.init_ai(bot.assistant)
        assert all(bot.store.one('SELECT deleted FROM notes WHERE id=?', (n['id'],))['deleted'] for n in expected)
        assert not bot.store.all('PRAGMA foreign_key_check')
        # Replaying the confirmation cannot widen or repeat the deletion.
        before = len(bot.telegram.sent)
        await bot.callback(1, None, state['inline_prompt_id'], state['actions']['Удалить'])
        assert len(bot.telegram.sent) == before
    asyncio.run(run())


def test_deleting_source_still_finds_followups_through_a_deleted_answer(bot):
    async def run():
        members, unrelated = await make_branch(bot)
        bot.notes.delete(1, members[2]['id'])
        deleted = bot.notes.delete(1, members[0]['id'])
        assert {n['id'] for n in deleted} == {n['id'] for n in members} - {members[2]['id']}
        assert bot.store.note(1, unrelated['id'])
        assert all(not bot.archive.directory(n).exists() for n in members)
    asyncio.run(run())


@pytest.mark.parametrize('selected', [0, 1])
@pytest.mark.parametrize('failure', ['locked', 'queued_ai', 'sending', 'storage'])
def test_branch_deletion_rechecks_every_member_and_rolls_back_before_removing_files(bot, monkeypatch, failure, selected):
    async def run():
        members, _ = await make_branch(bot)
        state = await confirm_delete(bot, members[selected]['message_id'])
        answer = members[-1] if selected == 0 else members[selected + 1]
        request = members[-2] if selected == 0 else members[selected]
        if failure == 'locked':
            await bot.note_locks[answer['id']].acquire()
        elif failure in {'queued_ai', 'sending'}:
            bot.ai_state.update_request(1, request['id'], **(
                {'state': 'queued'} if failure == 'queued_ai' else {'delivery': 'sending'}))
        else:
            delete = bot.store.delete
            calls = 0

            def fail(*args):
                nonlocal calls
                delete(*args)
                calls += 1
                if calls == 2:
                    raise OSError('disk failure')

            monkeypatch.setattr(bot.store, 'delete', fail)
        bot.telegram.sent.clear()
        try:
            with pytest.raises(OSError if failure == 'storage' else ValueError):
                await bot.callback(1, None, state['inline_prompt_id'], state['actions']['Удалить'])
        finally:
            if failure == 'locked':
                bot.note_locks[answer['id']].release()
        assert all(bot.store.note(1, n['id']) for n in members)
        assert all(bot.archive.directory(n).exists() for n in members)
        assert bot.ai_state.request(1, members[1]['id'])['response'] == 'Первый ответ'
        assert not any(m == 'deleteMessage' for m, _ in bot.telegram.sent)
    asyncio.run(run())


@pytest.mark.parametrize('selected', [0, 1, 3, 4])
def test_ai_delete_uses_same_scope_and_keeps_its_running_command(bot, selected):
    async def run():
        members, unrelated = await make_branch(bot)
        note = members[selected]
        request = await receive(bot, 'Удали это', message_id=12001, reply=min(receipts(bot, [note])))
        bot.assistant.responses = [step('read_note', note_id=note['id']),
            step('delete_note', note_id=note['id'], revision=note['revision']), step(message='Удалено')]
        await bot.process_ai(bot.ai_state.claim())
        expected = {n['id'] for n in {0: members, 1: members[1:3], 3: members[3:5]}.get(selected, [note])}
        deleted = {n['id'] for n in bot.store.all('SELECT id FROM notes WHERE owner=1 AND deleted=1')}
        assert deleted == expected
        assert bot.store.note(1, unrelated['id'])
        assert bot.ai_state.request(1, request['id'])['state'] == 'ready'
        jobs = bot.store.all('SELECT * FROM ai_message_jobs WHERE request_id=?', (request['id'],))
        assert {j['note_id'] for j in jobs} == expected and all(j['state'] == 'done' for j in jobs)
        before = len(bot.telegram.sent)
        await bot.ai_note_actions.execute(1, request['id'], 1)
        assert len(bot.telegram.sent) == before
    asyncio.run(run())


@pytest.mark.parametrize('via_ai', [False, True])
def test_first_ai_question_deletes_only_its_answer_and_keeps_later_turns(bot, via_ai):
    async def run():
        folder = bot.store.current_folder(1)
        bot.ai_state.mode(1, folder, 'chat')
        source = await receive(bot, 'Первая тема', message_id=10000)
        await bot.process_ai(bot.ai_state.claim())
        first = bot.ai_state.request(1, source['id'])
        next_question = await receive(bot, 'Уточнение', message_id=10001)
        await bot.process_ai(bot.ai_state.claim())
        if via_ai:
            await receive(bot, 'Удали исходный вопрос', message_id=10002)
            bot.assistant.responses = [step('read_note', note_id=source['id']),
                step('delete_note', note_id=source['id'], revision=source['revision']), step(message='Удалено')]
            await bot.process_ai(bot.ai_state.claim())
            deleted = bot.store.all('SELECT id FROM notes WHERE owner=1 AND deleted=1')
        else:
            deleted = bot.notes.delete(1, source['id'])
        assert {n['id'] for n in deleted} == {source['id'], first['result_note_id']}
        followup = bot.ai_state.request(1, next_question['id'])
        assert followup['state'] == 'ready' and followup['response']
        assert bot.store.note(1, followup['result_note_id'])
        assert bot.store.note(1, next_question['id'])['transcript'] == 'Уточнение'
    asyncio.run(run())


def test_deleting_answer_cancels_pending_delivery_without_losing_the_question(bot):
    async def run():
        members, _ = await make_branch(bot)
        request = members[-2]
        bot.ai_state.update_request(1, request['id'], delivery='pending')
        bot.notes.delete(1, members[-1]['id'])
        current = bot.ai_state.request(1, request['id'])
        assert current['delivery'] == 'cancelled' and current['response'] is None
        assert bot.store.note(1, request['id'])['transcript'] == request['transcript']
        before = len(bot.telegram.sent)
        await bot.deliver_ai(current)
        assert len(bot.telegram.sent) == before
    asyncio.run(run())


@pytest.mark.parametrize('link', ['result_note_id', 'ai_generated_from'])
def test_request_deletion_follows_exact_result_links_and_includes_additional_outputs(bot, link):
    from voice_memory_bot.ai_store import result_text

    async def run():
        members, unrelated = await make_branch(bot)
        request, answer = members[1:3]
        with bot.store.transaction():
            if link == 'result_note_id':
                bot.store.db.execute('UPDATE notes SET ai_generated_from=NULL WHERE id=?', (answer['id'],))
            else:
                bot.store.db.execute('UPDATE ai_requests SET result_note_id=NULL WHERE note_id=?', (request['id'],))
            extra = result_text(bot.store, request, 'Дополнительный результат этого запроса')
        bot.messages.record(extra, 13000, None, 'ai_response')
        bot.flush_ai_archive()
        expected = {request['id'], answer['id'], extra['id']}
        # Selecting the request and an already included answer in a batch deduplicates them.
        scope = bot.notes.deletion_branch(1, [request['id'], answer['id']])
        assert {n['id'] for n in scope.notes} == expected and len(scope.notes) == 3
        deleted = bot.notes.delete(1, request['id'])
        assert {n['id'] for n in deleted} == expected
        assert all(not bot.archive.directory(n).exists() for n in deleted)
        assert all(bot.store.note(1, n['id']) for n in [members[0], *members[3:], unrelated])
        bot.init_ai(bot.assistant)
        assert all(bot.store.one('SELECT deleted FROM notes WHERE id=?', (identity,))['deleted']
                   for identity in expected)
    asyncio.run(run())
