import asyncio

import pytest
from support.base import make_bot
from support.commands import bot as bot
from support.commands import receive


async def open_move(bot, target=100, command='/move'):
    await bot.handle({'message': {'message_id': 400, 'from': {'id': 1},
        'chat': {'id': 1, 'type': 'private'}, 'text': command,
        'reply_to_message': {'message_id': target}}})
    return bot.store.one('SELECT * FROM reply_moves WHERE owner=1 ORDER BY rowid DESC')


async def prepare(bot):
    bot.telegram.message_sequence = 1000
    await receive(bot, 'Запись для переноса', 100)
    note = bot.store.notes(1)[0]
    bot.store.update_note(1, note['id'], state='ready')
    bot.messages.record(note, 200, None, 'transcript')
    return note


@pytest.mark.parametrize('new_folder', [False, True])
def test_move_reply_to_existing_or_new_folder(bot, new_folder):
    async def run():
        note = await prepare(bot)
        other = bot.store.create_folder(1, 'Другая')
        session = await open_move(bot, target=200, command='/move@your_audio_notes_bot')
        menu = [p for m, p in bot.telegram.sent if m == 'sendMessage'][-1]
        labels = [b['text'] for row in menu['keyboard']['keyboard'] for b in row]
        assert {'Другая', '＋ Новая папка', 'Отмена'} <= set(labels)
        assert 'inline_keyboard' not in menu['keyboard']
        assert all('style' not in b for row in menu['keyboard']['keyboard'] for b in row)
        prompt = session['prompt_id']
        if new_folder:
            await receive(bot, '＋ Новая папка', 450)
            menu = [p for m, p in bot.telegram.sent if m == 'sendMessage'][-1]
            assert menu['keyboard']['keyboard'] == [[{'text': 'Отмена'}]]
            await receive(bot, 'Созданная для переноса', 500)
            other = next(f for f in bot.store.folders(1) if f['name'] == 'Созданная для переноса')
        else:
            await receive(bot, 'Другая', 450)
        assert bot.store.note(1, note['id'])['folder_id'] == other['id']
        assert bot.store.note(1, note['id'])['transcript'] == 'Запись для переноса'
        deleted = {p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage'}
        assert {100, 200, prompt} <= deleted
        assert 400 not in deleted
        assert bot.store.one('SELECT * FROM reply_moves') is None
        assert bot.store.action(1) is None
    asyncio.run(run())


@pytest.mark.parametrize('new_folder', [False, True])
def test_cancel_preserves_reply_command_even_after_restart(bot, tmp_path, new_folder):
    async def run():
        note = await prepare(bot)
        session = await open_move(bot)
        if new_folder:
            await bot.callback(1, None, session['prompt_id'], f'rnew:{session["token"]}')
        session = bot.store.one("SELECT * FROM reply_moves WHERE owner=1")
        restarted = make_bot(tmp_path, bot.store)
        await restarted.callback(1, None, session['prompt_id'], f'rcancel:{session["token"]}')
        deleted = {p['message_id'] for m, p in restarted.telegram.sent if m == 'deleteMessage'}
        assert session['prompt_id'] in deleted
        assert 400 not in deleted
        assert bot.store.note(1, note['id'])['folder_id'] == note['folder_id']
        assert bot.store.action(1) is None
        await restarted.callback(1, None, session['prompt_id'], f'rnew:{session["token"]}')
        assert bot.store.action(1) is None
    asyncio.run(run())


def test_move_target_owner_isolation_and_pagination(bot):
    async def run():
        note = await prepare(bot)
        foreign = bot.store.create_folder(2, 'Чужая')
        for number in range(10):
            bot.store.create_folder(1, f'Очень длинное название папки {number}')
        session = await open_move(bot)
        assert {'＋ Новая папка', 'Отмена', '→'} <= set(bot.bottom_state(1)['actions'])
        await bot.callback(2, None, session['prompt_id'], f'rcancel:{session["token"]}')
        assert bot.store.one('SELECT * FROM reply_moves')
        with pytest.raises(ValueError):
            await bot.callback(1, None, session['prompt_id'], f'rmove:{session["token"]}:{foreign["id"]}')
        assert bot.store.note(1, note['id'])['folder_id'] == note['folder_id']
        await bot.callback(1, None, session['prompt_id'], f'rpage:{session["token"]}:1')
        markup = [p for m, p in bot.telegram.sent if m == 'sendMessage'][-1]['keyboard']
        assert any(b['text'] == 'Отмена' for row in markup['keyboard'] for b in row)
    asyncio.run(run())


def test_folder_buttons_fit_short_medium_and_long_labels():
    from voice_memory_bot.reply_move import folder_button_rows
    buttons = [{"text": name, "callback_data": str(i)} for i, name in enumerate([
        "Дом", "Работа", "Идеи", "Планы на год", "Мои проекты", "Очень длинное название папки",
    ])]
    rows = folder_button_rows(buttons)
    assert [len(row) for row in rows] == [3, 2, 1]
    assert [item for row in rows for item in row] == buttons


def test_regular_create_has_only_cancel(bot):
    async def run():
        await receive(bot, '/new', 100)
        menu = [p for m, p in bot.telegram.sent if m == 'sendMessage'][-1]
        assert menu['keyboard']['keyboard'] == [[{'text': 'Отмена'}]]
        await receive(bot, 'Отмена', 101)
        assert bot.store.action(1) is None
        assert not bot.store.notes(1)
    asyncio.run(run())
