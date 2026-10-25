import asyncio

import pytest
from test_commands import bot as bot
from test_commands import receive


@pytest.mark.parametrize('command', ['/ai', '/move', '/delete'])
def test_menu_uses_latest_user_voice_in_active_folder(bot, command):
    async def run():
        await receive(bot, 'Старое сообщение', 100)
        source = bot.store.current_folder(1)
        voice, _ = bot.store.add_note(1, source, {'message_id': 200}, 'voice',
                                     transcript='Последняя расшифровка')
        bot.store.update_note(1, voice['id'], state='ready', transcript_message_id=201)
        other = bot.store.create_folder(1, 'Другая')
        elsewhere, _ = bot.store.add_note(1, other['id'], {'message_id': 300}, 'text', transcript='Другая папка')
        generated, _ = bot.store.add_note(1, source, {'message_id': 400}, 'text', transcript='Ответ ИИ')
        with bot.store.db:
            bot.store.db.execute("UPDATE notes SET origin='ai' WHERE id=?", (generated['id'],))
        await receive(bot, command, 500)
        assert bot.store.note(1, elsewhere['id'])
        assert bot.store.note(1, generated['id'])
        if command == '/delete':
            assert bot.store.one('SELECT deleted FROM notes WHERE id=?', (voice['id'],))['deleted'] == 1
        elif command == '/move':
            assert bot.store.one('SELECT note_id FROM reply_moves')['note_id'] == voice['id']
        else:
            assert bot.store.one('SELECT note_id,content FROM ai_note_context') == {
                'note_id': voice['id'], 'content': 'Последняя расшифровка'}
    asyncio.run(run())


@pytest.mark.parametrize('target', [100, 999])
@pytest.mark.parametrize('command', ['/ai', '/move', '/delete'])
def test_explicit_reply_never_falls_back(bot, target, command):
    async def run():
        await receive(bot, 'Первая', 100)
        first = bot.store.notes(1)[0]
        await receive(bot, 'Последняя', 200)
        latest = next(n for n in bot.store.notes(1) if n['message_id'] == 200)
        await bot.handle({'message': {'message_id': 300, 'from': {'id': 1},
            'chat': {'id': 1, 'type': 'private'}, 'text': command,
            'reply_to_message': {'message_id': target}}})
        assert bot.store.note(1, latest['id'])
        if target == 999:
            assert bot.store.note(1, first['id'])
            assert not bot.store.all('SELECT * FROM ai_note_context')
            assert not bot.store.all('SELECT * FROM reply_moves')
        elif command == '/ai':
            assert bot.store.one('SELECT note_id FROM ai_note_context')['note_id'] == first['id']
        elif command == '/move':
            assert bot.store.one('SELECT note_id FROM reply_moves')['note_id'] == first['id']
        else:
            assert bot.store.one('SELECT deleted FROM notes WHERE id=?', (first['id'],))['deleted']
    asyncio.run(run())


@pytest.mark.parametrize('command', ['/ai', '/move', '/delete'])
def test_empty_active_folder_does_not_target_other_folder(bot, command):
    async def run():
        await receive(bot, 'Сохранить', 100)
        note = bot.store.notes(1)[0]
        folder = bot.store.create_folder(1, 'Пустая')
        bot.store.select(1, folder['id'])
        await receive(bot, command, 200)
        assert bot.store.note(1, note['id'])
        assert not bot.store.all('SELECT * FROM ai_note_context')
        assert not bot.store.all('SELECT * FROM reply_moves')
    asyncio.run(run())
