import asyncio

import pytest
from support.commands import bot as bot
from support.commands import receive
from support.menus import reply_keyboard


@pytest.mark.parametrize('inline', [False, True])
@pytest.mark.parametrize('reply', [100, None])
@pytest.mark.parametrize('label,action', [('Изменить с ИИ', 'aiask'),
                                         ('Перенести', 'recordmove'),
                                         ('Удалить', 'delete')])
def test_options_pin_target_and_dispatch(bot, reply, label, action, inline):
    async def run():
        await receive(bot, 'Первая', 100)
        await receive(bot, 'Вторая', 200)
        expected = next(n for n in bot.store.notes(1) if n['message_id'] == (reply or 200))
        message = {'message_id': 300, 'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'},
                   'text': '/Options'}
        if reply:
            message['reply_to_message'] = {'message_id': reply}
        await bot.handle({'message': message})
        state = bot.bottom_state(1)
        assert state['text'] == 'Что сделать с этой записью?'
        assert state['actions'][label] == f"{action}:{expected['id']}"
        prompt = next(p for m, p in reversed(bot.telegram.sent) if m == 'sendMessage')
        inline_keys = prompt['keyboard']['inline_keyboard']
        assert [[b['text'] for b in row] for row in inline_keys] == [
            [b['text'] for b in row] for row in reply_keyboard(bot)['keyboard']]
        assert prompt['reply_to'] == reply
        # Receiving another record cannot redirect an already displayed action.
        bot.store.add_note(1, expected['folder_id'], {'message_id': 400}, 'text', transcript='Новая')
        if inline:
            await bot.callback(1, None, state['inline_prompt_id'], state['actions'][label])
        else:
            await receive(bot, label, 500)
        if action == 'aiask':
            assert bot.store.one('SELECT note_id FROM ai_note_context')['note_id'] == expected['id']
        elif action == 'recordmove':
            assert bot.store.one('SELECT note_id FROM reply_moves')['note_id'] == expected['id']
        else:
            keys = next(p for m, p in reversed(bot.telegram.sent) if m == 'sendMessage')['keyboard']['inline_keyboard']
            assert keys[0][0]["callback_data"] == bot.bottom_state(1)["actions"]["Удалить"]
            assert bot.bottom_state(1)["screen"] == f"notedelete:{expected['id']}"
            assert bot.bottom_state(1)['reply_to'] == (reply or 200)
            assert bot.store.note(1, expected['id'])
        deleted = {p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage'}
        assert (300 in deleted) == (reply is None)
    asyncio.run(run())


@pytest.mark.parametrize('reply', [None, 999])
def test_options_do_not_fall_back_outside_selection(bot, reply):
    async def run():
        await receive(bot, 'Сохранить', 100)
        empty = bot.store.create_folder(1, 'Пустая')
        bot.store.select(1, empty['id'])
        message = {'message_id': 200, 'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'},
                   'text': '/options'}
        if reply:
            message['reply_to_message'] = {'message_id': reply}
        await bot.handle({'message': message})
        assert not (bot.bottom_state(1) or {}).get('screen', '').startswith('recordoptions:')
        assert len(bot.store.notes(1)) == 1
    asyncio.run(run())


def test_compact_options_cancel_restores_folder_keyboard(bot):
    async def run():
        await receive(bot, 'Запись', 100)
        await receive(bot, '/options', 200)
        keys = reply_keyboard(bot)['keyboard']
        assert [[b['text'] for b in row] for row in keys] == [
            ['Изменить с ИИ', 'Перенести'], ['Удалить', 'Отмена']]
        await receive(bot, 'Отмена', 300)
        assert bot.bottom_state(1)['screen'] == 'root'
    asyncio.run(run())
