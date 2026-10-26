import asyncio

import pytest
from support.ai import bot as bot
from support.ai import receive, step

from voice_memory_bot.providers import ProviderError


async def answer(bot, text, message_id, reply, result):
    note = await receive(bot, text, message_id=message_id, reply=reply)
    bot.assistant.responses = [result]
    await bot.process_ai(bot.ai_state.claim())
    request = bot.ai_state.request(1, note['id'])
    saved = bot.store.note(1, request['result_note_id']) if request['result_note_id'] else None
    return note, request, saved


def test_all_answers_and_requests_remain_saved_searchable_and_exportable(bot):
    async def run():
        source = await receive(bot, 'Исходная запись', message_id=10000)
        first, first_request, old = await answer(bot, 'Обсуди', 10001, source['message_id'], step(message='Первый ответ'))
        old_messages = bot.ai_state.message_ref(1, old['id'])['telegram_messages']
        old_message_id = old_messages[0]['message_id']
        old_files = {p.name: p.read_bytes() for p in bot.archive.directory(old).iterdir()}
        separate_input, _, separate = await answer(bot, 'Отдельное обсуждение', 10002, 99999, step(message='Другой ответ'))
        latest_input, _, latest = await answer(bot, 'Уточни', 10003, old_message_id, step(message='Последний ответ'))
        expected = {n['id'] for n in (source, first, old, separate_input, separate, latest_input, latest)}
        assert {n['id'] for n in bot.store.notes(1, limit=-1)} == expected
        assert bot.store.notes(1, query='Первый ответ')[0]['id'] == old['id']
        assert bot.store.note(1, old['id'])['entry_type'] == 'note'
        assert bot.ai_state.message_ref(1, old['id'])['telegram_messages'] == old_messages
        assert {p.name: p.read_bytes() for p in bot.archive.directory(old).iterdir()} == old_files
        assert not any(m == 'deleteMessage' and p['message_id'] == old_message_id for m, p in bot.telegram.sent)
        exported = bot.store.export_notes(1)
        assert {n['id'] for n in exported} == expected
        assert 'ai_response' not in next(n for n in exported if n['id'] == first['id'])
        bot.init_ai(bot.assistant)
        await bot.ai_engine.run(first_request)  # Replaying an old completion must not hide the latest answer.
        assert bot.store.note(1, latest['id'])['entry_type'] == 'note'
        newest_input, _, newest = await answer(bot, 'Продолжи старую мысль', 10004, old_message_id, step(message='Новый итог'))
        payload = bot.assistant.calls[-1][0]
        assert payload['reply_note_id'] == old['id']
        assert payload['history'][0]['assistant'] == 'Первый ответ'
        assert old['id'] in {n['id'] for n in payload['conversation_messages']}
        assert {n['id'] for n in bot.store.notes(1, limit=-1)} == expected | {newest_input['id'], newest['id']}
        assert bot.store.folders(1)[0]['count'] == 9
    asyncio.run(run())


def test_failed_request_and_operation_confirmation_keep_last_substantive_answer(bot):
    async def run():
        source = await receive(bot, 'Исходная запись', message_id=10000)
        _, _, old = await answer(bot, 'Обсуди', 10001, source['message_id'], step(message='Содержательный ответ'))
        message = bot.ai_state.message_ref(1, old['id'])['telegram_messages'][0]['message_id']
        await answer(bot, 'Уточни', 10002, message, ProviderError('unavailable'))
        assert bot.store.note(1, old['id'])['entry_type'] == 'note'
        folder = bot.store.create_folder(1, 'Работа')
        request = await receive(bot, 'Перенеси исходник', message_id=10003, reply=message)
        bot.assistant.responses = [step('read_note', note_id=source['id']),
            step('move_note', note_id=source['id'], revision=source['revision'], folder_id=folder['id']),
            step(message='Перенесено')]
        await bot.process_ai(bot.ai_state.claim())
        assert bot.ai_state.request(1, request['id'])['result_note_id'] is None
        assert bot.store.note(1, old['id'])['entry_type'] == 'note'
    asyncio.run(run())


def test_new_answer_storage_failure_leaves_previous_saved_answer(bot, monkeypatch):
    from voice_memory_bot import ai_store

    async def run():
        source = await receive(bot, 'Исходник', message_id=10000)
        _, _, old = await answer(bot, 'Обсуди', 10001, source['message_id'], step(message='Старый итог'))
        message = bot.ai_state.message_ref(1, old['id'])['telegram_messages'][0]['message_id']
        current = await receive(bot, 'Уточни', message_id=10002, reply=message)
        create = ai_store.result_text
        def interrupted(*args):
            create(*args)
            raise OSError('storage interruption')
        monkeypatch.setattr(ai_store, 'result_text', interrupted)
        with pytest.raises(OSError):
            bot.ai_state.finish_reply(1, current['id'], 'Новый итог')
        assert bot.store.note(1, old['id'])['entry_type'] == 'note'
        assert bot.ai_state.request(1, current['id'])['result_note_id'] is None
    asyncio.run(run())
