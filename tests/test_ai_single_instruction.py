import asyncio
from dataclasses import replace

import pytest
from support.ai import bot as bot
from support.ai import receive
from support.menus import reply_keyboard


def message(identity, text=None, *, voice=False, reply=None):
    value = {'message_id': identity, 'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'}}
    if voice:
        value['voice'] = {'file_id': 'voice', 'file_size': 32044, 'duration': 1}
    else:
        value['text'] = text
    if reply:
        value['reply_to_message'] = {'message_id': reply}
    return {'message': value}


@pytest.mark.parametrize('instruction_reply', [None, 'source', 'prompt'])
@pytest.mark.parametrize('entry', ['command', 'reply_command', 'button'])
@pytest.mark.parametrize('voice', [False, True])
def test_ai_instruction_is_consumed_before_processing_and_saved_answer_can_be_replied_to(bot, entry, voice, instruction_reply):
    async def run():
        await bot.show_home(1)
        source = await receive(bot, 'Исходная мысль', 1000)
        old_conversation = bot.ai_state.start_note_conversation(1, source['id'])
        bot.ai_state.link(1, source['message_id'], old_conversation)
        bottom, keyboard = bot.bottom_state(1), reply_keyboard(bot)
        if entry == 'button':
            await bot.callback(1, None, source['message_id'], f"aiask:{source['id']}")
        else:
            await bot.handle(message(1001, '/ai', reply=source['message_id'] if entry == 'reply_command' else None))
        prompt = next(p for m, p in reversed(bot.telegram.sent) if m == 'sendMessage')
        conversation = bot.store.action(1).split(':', 1)[1]
        prompt_id = bot.store.one("SELECT message_id FROM ai_instruction_prompts WHERE conversation_id=?", (conversation,))["message_id"]
        assert prompt['text'] == 'Отправь инструкцию для ИИ текстом или голосовым.'
        expected_reply = (source['transcript_message_id'] or source['message_id']) if entry == 'command' else source['message_id']
        assert prompt['reply_to'] == expected_reply
        assert prompt['keyboard'] == {'inline_keyboard': [[{
            'text': 'Отмена', 'callback_data': f'aicancel:{conversation}'}]]}
        assert bot.bottom_state(1) == bottom and reply_keyboard(bot) == keyboard
        assert bot.ai_state.claim() is None

        reply = {'source': source['message_id'], 'prompt': prompt_id}.get(instruction_reply)
        await bot.handle(message(1002, 'Сделай короче', voice=voice, reply=reply))
        assert bot.store.action(1) is None
        assert any(method == 'editMessageReplyMarkup' and payload['message_id'] == prompt_id
                   and payload['reply_markup'] == {'inline_keyboard': []}
                   for method, payload in bot.telegram.sent)
        instruction = bot.store.one('SELECT * FROM notes WHERE owner=1 AND message_id=1002')
        request = bot.ai_state.request(1, instruction['id'])
        assert request['conversation_id'] == conversation
        assert request['reply_to'] == expected_reply
        # A rapid next message is a note even before voice transcription or the AI response.
        bot.init_ai(bot.assistant)
        await bot.handle(message(1003, 'Отдельная заметка'))
        plain = bot.store.one('SELECT * FROM notes WHERE owner=1 AND message_id=1003')
        assert plain['entry_type'] == 'note' and bot.ai_state.request(1, plain['id']) is None
        while queued := bot.store.claim_note():
            await bot.process(queued)
        await bot.process_ai(bot.ai_state.claim())
        request = bot.ai_state.request(1, instruction['id'])
        progress = next(p for m, p in bot.telegram.sent if m == 'sendMessage' and p['text'] == 'Думаю…')
        assert progress['reply_to'] == expected_reply
        saved = bot.store.note(1, request['result_note_id'])
        assert saved['origin'] == 'ai' and saved['entry_type'] == 'note'
        assert bot.assistant.calls[-1][0]['selected_note']['id'] == source['id']
        answer_id = bot.ai_state.message_ref(1, saved['id'])['telegram_messages'][0]['message_id']
        followup = await receive(bot, 'Уточни ответ', 1004, reply=answer_id)
        assert bot.ai_state.request(1, followup['id'])['conversation_id'] == conversation
        await bot.process_ai(bot.ai_state.claim())
        assert bot.assistant.calls[-1][0]['history'][0]['assistant'] == 'Ответ'
        final = await receive(bot, 'Ещё одна заметка', 1005)
        assert final['entry_type'] == 'note' and bot.ai_state.request(1, final['id']) is None
        assert bot.bottom_state(1) == bottom and reply_keyboard(bot) == keyboard
    asyncio.run(run())


def test_rejected_instruction_keeps_inline_cancel_and_does_not_change_bottom_menu(bot):
    async def run():
        await bot.show_home(1)
        source = await receive(bot, 'Исходник', 1000)
        await bot.handle(message(1001, '/ai', reply=source['message_id']))
        action, bottom, keyboard = bot.store.action(1), bot.bottom_state(1), reply_keyboard(bot)
        bot.store.limits = replace(bot.store.limits, max_pending_per_user=0)
        await bot.handle(message(1002, 'Инструкция'))
        assert bot.store.action(1) == action
        assert bot.store.one('SELECT id FROM notes WHERE message_id=1002') is None
        prompt = bot.telegram.sent[-1][1]
        assert 'Очередь ИИ заполнена' in prompt['text']
        assert prompt['keyboard']['inline_keyboard'][0][0]['callback_data'] == 'aicancel:' + action.split(':')[1]
        assert bot.bottom_state(1) == bottom and reply_keyboard(bot) == keyboard
    asyncio.run(run())


def test_instruction_state_and_request_commit_together(bot, monkeypatch):
    async def run():
        source = await receive(bot, 'Исходник', 1000)
        await bot.callback(1, None, source['message_id'], f"aiask:{source['id']}")
        action = bot.store.action(1)
        incoming = message(1001, 'Инструкция')['message']
        note, _ = bot.store.add_note(1, source['folder_id'], incoming, 'text', transcript='Инструкция')

        def failed_link(*args):
            raise RuntimeError('interrupted capture')

        monkeypatch.setattr(bot.ai_state, 'link', failed_link)
        with pytest.raises(RuntimeError, match='interrupted capture'):
            bot.capture_ai_input(note, incoming, scoped=True)
        assert bot.store.action(1) == action and bot.ai_state.request(1, note['id']) is None
        assert bot.store.note(1, note['id'])['entry_type'] == 'note'
    asyncio.run(run())
