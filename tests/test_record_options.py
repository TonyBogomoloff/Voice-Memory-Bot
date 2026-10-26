import asyncio

import pytest
from support.commands import bot as bot
from support.commands import receive

from voice_memory_bot.providers import ProviderError, TelegramError


@pytest.mark.parametrize('inline', [False, True])
@pytest.mark.parametrize('reply', [100, None])
@pytest.mark.parametrize('label,action', [('Изменить с ИИ', 'aiask'),
                                         ('Перенести', 'recordmove'),
                                         ('Удалить', 'delete')])
def test_options_pin_target_and_dispatch(bot, reply, label, action, inline):
    async def run():
        bot.telegram.message_sequence = 1000
        await receive(bot, 'Первая', 100)
        await receive(bot, 'Вторая', 200)
        expected = next(n for n in bot.store.notes(1) if n['message_id'] == (reply or 200))
        bot.messages.record(expected, 600, None, 'view')
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
        assert {b['text'] for row in inline_keys for b in row} == set(state['actions'])
        assert 'keyboard' not in prompt['keyboard']
        target = reply or 600
        assert prompt['reply_to'] == target
        assert state['reply_to'] == target
        options_id = state['inline_prompt_id']
        transition_start = len(bot.telegram.sent)
        # Receiving another record cannot redirect an already displayed action.
        bot.store.add_note(1, expected['folder_id'], {'message_id': 400}, 'text', transcript='Новая')
        if inline:
            await bot.callback(1, None, state['inline_prompt_id'], state['actions'][label])
        else:
            await receive(bot, label, 500)
        if action == 'aiask':
            assert bot.store.one('SELECT note_id FROM ai_note_context')['note_id'] == expected['id']
            instruction = next(p for m, p in reversed(bot.telegram.sent) if m == 'sendMessage')
            assert instruction['text'] == 'Отправь инструкцию для ИИ текстом или голосовым.'
            assert instruction['reply_to'] == target
            assert bot.store.one('SELECT reply_to FROM ai_instruction_prompts')['reply_to'] == target
        elif action == 'recordmove':
            assert bot.store.one('SELECT note_id FROM reply_moves')['note_id'] == expected['id']
        else:
            keys = next(p for m, p in reversed(bot.telegram.sent) if m == 'sendMessage')['keyboard']['inline_keyboard']
            assert keys[0][0]["callback_data"] == bot.bottom_state(1)["actions"]["Удалить"]
            assert bot.bottom_state(1)["screen"] == f"notedelete:{expected['id']}"
            assert bot.bottom_state(1)['reply_to'] == target
            assert bot.store.note(1, expected['id'])
        deleted = {p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage'}
        assert (300 in deleted) == (reply is None)
        assert options_id in deleted and target not in deleted
        transition = bot.telegram.sent[transition_start:]
        delivered = next(i for i, (m, _) in enumerate(transition) if m == 'sendMessage')
        removed = next(i for i, (m, p) in enumerate(transition)
                       if m == 'deleteMessage' and p['message_id'] == options_id)
        assert delivered < removed
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
        keys = next(p for m, p in reversed(bot.telegram.sent) if m == 'sendMessage')['keyboard']['inline_keyboard']
        assert [[b['text'] for b in row] for row in keys] == [
            ['Изменить с ИИ', 'Перенести'], ['Удалить', 'Отмена']]
        await receive(bot, 'Отмена', 300)
        assert bot.bottom_state(1)['screen'] == 'root'
    asyncio.run(run())


@pytest.mark.parametrize('stage', ['options', 'ai'])
def test_options_and_ai_recover_missing_target_without_losing_record(bot, stage):
    async def run():
        bot.telegram.message_sequence = 1000
        await receive(bot, 'Исходная запись', 100)
        note = bot.store.notes(1)[0]
        await receive(bot, '/options', 200)
        state = bot.bottom_state(1)
        original = bot.telegram.text
        delivered = []

        async def missing_source(owner, text, thread=None, keyboard=None, reply_to=None):
            if reply_to == 100:
                raise TelegramError(400, 'sendMessage', 'message to be replied not found')
            result = await original(owner, text, thread, keyboard, reply_to)
            delivered.append((result['message_id'], text, reply_to))
            return result

        bot.telegram.text = missing_source
        if stage == 'options':
            await receive(bot, '/options', 300)
            state = bot.bottom_state(1)
        else:
            await bot.callback(1, None, state['inline_prompt_id'], state['actions']['Изменить с ИИ'])
        preview, prompt = delivered
        assert preview[1] == 'Запись из архива:\nИсходная запись'
        assert prompt[2] == preview[0]
        assert bot.reply_note(1, {'reply_to_message': {'message_id': preview[0]}})['id'] == note['id']
        if stage == 'options':
            assert state['reply_to'] == preview[0]
        else:
            assert bot.store.one('SELECT reply_to FROM ai_instruction_prompts')['reply_to'] == preview[0]
        assert bot.store.note(1, note['id'])['transcript'] == 'Исходная запись'
    asyncio.run(run())


def test_failed_ai_instruction_keeps_options_menu(bot):
    async def run():
        bot.telegram.message_sequence = 1000
        await receive(bot, 'Исходник', 100)
        await receive(bot, '/options', 200)
        state = bot.bottom_state(1)
        bot.telegram.sent.clear()
        bot.telegram.fail_delivery = True
        with pytest.raises(ProviderError, match='telegram_transport_unknown'):
            await bot.callback(1, None, state['inline_prompt_id'], state['actions']['Изменить с ИИ'])
        assert [m for m, _ in bot.telegram.sent] == ['sendMessage']
        assert bot.bottom_state(1) == state
        assert bot.store.one('SELECT message_id FROM ai_instruction_prompts') is None
    asyncio.run(run())
