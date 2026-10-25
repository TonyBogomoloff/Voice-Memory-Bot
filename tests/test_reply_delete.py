import asyncio

import pytest
from support.commands import bot as bot
from support.commands import receive
from support.menus import labels


@pytest.mark.parametrize('target', [100, 200, None, 300, 999])
@pytest.mark.parametrize('command', ['/delete', '/delete@your_audio_notes_bot'])
def test_reply_delete_only_owned_record_after_confirmation(bot, target, command):
    async def run():
        await receive(bot, 'Сохранённая заметка', 100)
        note = bot.store.notes(1)[0]
        bot.messages.record(note, 200, None, 'transcript')
        directory = bot.archive.directory(note)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "note.md").write_text("Сохранённая заметка")
        bot.store.add_note(2, bot.store.ensure_user(2), {'message_id': 300}, 'text', transcript='Чужая')
        bot.store.set_action(1, 'create')
        message = {'message_id': 400, 'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'}, 'text': command}
        if target:
            message['reply_to_message'] = {'message_id': target}
        bot.telegram.sent.clear()
        await bot.handle({'message': message})
        assert len(bot.store.notes(1)) == 1
        assert len(bot.store.notes(2)) == 1
        assert directory.exists()
        sent = [p for m, p in bot.telegram.sent if m == 'sendMessage']
        if target in {100, 200, None}:
            assert sent[-1]['text'] == 'Удалить запись?'
            assert sent[-1]['reply_to'] == (target or 200)
            inline = sent[-1]['keyboard']['inline_keyboard']
            assert [[b['text'] for b in row] for row in inline] == [['Удалить', 'Отмена']]
            assert [[b['text'] for b in row] for row in sent[-2]['keyboard']['keyboard']] == [['Удалить', 'Отмена']]
            state = bot.bottom_state(1)
            await bot.callback(1, None, state['inline_prompt_id'], inline[0][0]['callback_data'])
            assert not bot.store.notes(1)
            assert not directory.exists()
            notice_index = next(i for i, (m, p) in enumerate(bot.telegram.sent)
                                if m == 'sendMessage' and p.get('text') == 'Сообщение удалено.')
            assert all(i > notice_index for i, (m, p) in enumerate(bot.telegram.sent)
                       if m == 'deleteMessage' and p['message_id'] in {100, 200})
            assert bot.store.one('SELECT transcript FROM notes WHERE id=?', (note['id'],))['transcript'] is None
        deleted = {p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage'}
        assert 300 not in deleted
        if target is not None:
            assert 400 not in deleted
        if target in {100, 200, None}:
            assert {100, 200} <= deleted
            assert (400 in deleted) == (target is None)
        else:
            assert not ({100, 200, None} & deleted)
    asyncio.run(run())


def test_processing_record_cannot_be_deleted(bot):
    async def run():
        await receive(bot, 'Ещё обрабатывается', 100)
        note = bot.store.notes(1)[0]
        bot.store.update_note(1, note['id'], state='processing')
        await bot.handle({'message': {'message_id': 400, 'from': {'id': 1},
            'chat': {'id': 1, 'type': 'private'}, 'text': '/delete', 'reply_to_message': {'message_id': 100}}})
        assert bot.store.note(1, note['id'])
        assert any('Дождись завершения' in p.get('text', '') for m, p in bot.telegram.sent)
    asyncio.run(run())


def test_minus_removed_and_stale_button_does_not_create_note(bot):
    async def run():
        for command in ['/start', '/settings', '/folders', '➖']:
            await receive(bot, command)
            assert '➖' not in labels(bot)
        assert bot.store.notes(1) == []
    asyncio.run(run())


@pytest.mark.parametrize('inline', [False, True])
@pytest.mark.parametrize('cancel', [False, True])
def test_delete_confirmation_shared_target_and_single_use(bot, inline, cancel):
    async def run():
        bot.telegram.message_sequence = 1000
        await receive(bot, 'Первая', 100)
        first = bot.store.notes(1)[0]
        await receive(bot, '/delete', 200)
        state = bot.bottom_state(1)
        await receive(bot, 'Вторая', 300)
        label = 'Отмена' if cancel else 'Удалить'
        if inline:
            await bot.callback(1, None, state['inline_prompt_id'], state['actions'][label])
        else:
            await receive(bot, label, 400)
        assert len(bot.store.notes(1)) == (2 if cancel else 1)
        assert bot.store.one('SELECT deleted FROM notes WHERE id=?', (first['id'],))['deleted'] == (not cancel)
        assert bot.bottom_state(1)['screen'] == 'root'
        await bot.callback(1, None, state['inline_prompt_id'], state['actions']['Удалить'])
        assert len(bot.store.notes(1)) == (2 if cancel else 1)
    asyncio.run(run())


def test_delete_old_confirmation_cannot_delete_after_new_prompt_or_restart(bot, tmp_path):
    from support.base import make_bot
    async def run():
        bot.telegram.message_sequence = 1000
        await receive(bot, 'Запись', 100)
        await receive(bot, '/delete', 200)
        old = bot.bottom_state(1)
        await receive(bot, '/delete', 300)
        await bot.callback(1, None, old['inline_prompt_id'], old['actions']['Удалить'])
        assert len(bot.store.notes(1)) == 1
        state = bot.bottom_state(1)
        restarted = make_bot(tmp_path, bot.store)
        await restarted.callback(1, None, state['inline_prompt_id'], state['actions']['Удалить'])
        assert len(bot.store.notes(1)) == 1
    asyncio.run(run())
