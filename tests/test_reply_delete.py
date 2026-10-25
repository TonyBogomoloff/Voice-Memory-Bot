import asyncio

import pytest
from test_commands import bot as bot
from test_commands import receive
from test_folder_shortcuts import labels


@pytest.mark.parametrize('target', [100, 200, None, 300, 999])
@pytest.mark.parametrize('command', ['/delete', '/delete@your_audio_notes_bot'])
def test_reply_delete_only_owned_record_without_confirmation(bot, target, command):
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
        assert len(bot.store.notes(1)) == (0 if target in {100, 200, None} else 1)
        assert len(bot.store.notes(2)) == 1
        assert directory.exists() == (target not in {100, 200, None})
        sent = [p for m, p in bot.telegram.sent if m == 'sendMessage']
        assert not sent[-1].get('keyboard')
        if target in {100, 200, None}:
            assert sent[-1]['text'] == 'Сообщение удалено.'
            assert bot.store.one('SELECT transcript FROM notes WHERE id=?', (note['id'],))['transcript'] is None
        deleted = {p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage'}
        assert 300 not in deleted
        if target in {100, 200, None}:
            assert {100, 200} <= deleted
            assert 400 not in deleted
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
