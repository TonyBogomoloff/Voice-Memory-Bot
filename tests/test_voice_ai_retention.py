import asyncio
from dataclasses import replace

import pytest
from support.ai import bot as bot
from support.ai import ready_note, receive, step, tool
from support.note_actions import request_for

from voice_memory_bot.bottom_menu import ROOT
from voice_memory_bot.help_menu import HELP_TOPICS


@pytest.mark.parametrize('mode', ['notes', 'chat'])
@pytest.mark.parametrize('kind', ['voice', 'audio', 'video_note', 'document'])
def test_dictation_is_archived_without_implicit_ai_even_with_wake_enabled(bot, mode, kind):
    async def run():
        folder = bot.store.current_folder(1)
        bot.ai_state.mode(1, folder, mode)
        bot.ai_state.set_settings(1, wake_enabled=True)

        async def transcribe(path, **kwargs):
            return 'Луна, это текст моей заметки.'

        bot.groq.transcribe = transcribe
        await bot.handle({'message': {'message_id': 10001, 'from': {'id': 1},
            'chat': {'id': 1, 'type': 'private'}, kind: {
                'file_id': 'audio', 'file_size': 32044, 'duration': 1, 'file_name': 'audio.wav'}}})
        note = bot.store.one('SELECT * FROM notes WHERE message_id=10001 AND owner=1')
        assert note and bot.ai_state.request(1, note['id']) is None
        await bot.process(bot.store.claim_note())
        saved = bot.store.note(1, note['id'])
        assert saved['state'] == 'ready' and saved['transcript_delivery'] == 'sent'
        assert saved['folder_id'] == folder and saved['transcript'] == 'Луна, это текст моей заметки.'
        assert bot.archive.audio(saved).is_file()
        assert bot.store.notes(1, folder)[0]['id'] == saved['id']
        assert bot.ai_state.claim() is None and bot.ai_state.request(1, saved['id']) is None
        assert not bot.assistant.calls and not bot.assistant.address_calls
    asyncio.run(run())


def test_ai_backlog_does_not_block_ordinary_voice_in_chat_folder(bot):
    async def run():
        bot.ai_state.mode(1, bot.store.current_folder(1), 'chat')
        bot.store.limits = replace(bot.store.limits, max_pending_per_user=1)
        first = await receive(bot, 'Вопрос для ИИ', 10001)
        voice = await receive(bot, message_id=10002, voice=True)
        assert voice['state'] == 'ready' and bot.ai_state.request(1, voice['id']) is None
        assert bot.ai_state.claim()['note_id'] == first['id']
    asyncio.run(run())


@pytest.mark.parametrize('entry_type', ['ai', 'ai_history'])
def test_restored_record_can_be_explicitly_edited_without_changing_original(bot, entry_type):
    note = ready_note(bot, 'Исходная расшифровка')
    with bot.store.transaction():
        bot.store.db.execute('UPDATE notes SET entry_type=? WHERE id=?', (entry_type, note['id']))
    request = request_for(bot, note)
    tool(bot, request, step('read_note', note_id=note['id']))
    result = tool(bot, request, step('update_note', note_id=note['id'], revision=0,
                                    title='Правка', content='Отредактированный текст'))
    assert result['ok']
    saved = bot.store.note(1, note['id'])
    assert saved['transcript'] == 'Исходная расшифровка'
    assert saved['content'] == 'Отредактированный текст'


def test_legacy_voice_history_survives_help_refresh_restart_search_and_export(bot):
    async def run():
        bot.telegram.message_sequence = 20000
        folder = bot.store.current_folder(1)
        bot.ai_state.mode(1, folder, 'chat')
        conversation = bot.ai_state.active(1, folder)
        voices, answers = [], []
        for i in range(3):
            async def transcribe(path, **kwargs):
                return f'Голосовая запись номер {i}'

            bot.groq.transcribe = transcribe
            note = await receive(bot, message_id=10001+i, voice=True)
            bot.ai_state.enqueue(1, note['id'], 'folder', conversation=conversation)
            bot.assistant.responses = [step(message=f'Ответ номер {i}')]
            await bot.process_ai(bot.ai_state.claim())
            voices.append(bot.store.note(1, note['id']))
            answers.append(bot.store.note(1, bot.ai_state.request(1, note['id'])['result_note_id']))
        # Recreate the installed release's hidden history without changing any content.
        with bot.store.transaction():
            for answer in answers[:-1]:
                bot.store.db.execute("UPDATE notes SET entry_type='ai_history' WHERE id=?", (answer['id'],))
        before = bot.store.all('SELECT * FROM notes ORDER BY id')
        originals = {n['id']: bot.archive.audio(n).read_bytes() for n in voices}
        for mid, text in enumerate(('/help', HELP_TOPICS['ai'][0], ROOT), 11000):
            await bot.handle({'message': {'message_id': mid, 'from': {'id': 1},
                'chat': {'id': 1, 'type': 'private'}, 'text': text}})
        bot.init_ai(bot.assistant)
        bot.init_folder_view()
        bot.telegram.sent.clear()
        await bot.show_notes(1, None, folder)
        texts = [p.get('text') for method, p in bot.telegram.sent if method == 'sendMessage']
        for note in voices:
            assert note['transcript'] in texts
            assert bot.archive.audio(note).read_bytes() == originals[note['id']]
        for answer in answers:
            assert '🤖 ' + answer['transcript'] in texts
        expected = {n['id'] for n in voices + answers}
        assert {n['id'] for n in bot.store.notes(1, folder)} == expected
        assert {n['id'] for n in bot.store.export_notes(1, folder)} == expected
        assert bot.store.folders(1)[0]['count'] == 6
        assert bot.store.notes(1, query='Голосовая запись номер 0')[0]['id'] == voices[0]['id']
        assert bot.store.notes(1, query='Ответ номер 0')[0]['id'] == answers[0]['id']
        assert bot.store.all('SELECT * FROM notes ORDER BY id') == before
        assert len(bot.assistant.calls) == 3
        # Explicit deletions stay deleted, including the old reply stored on its request.
        bot.store.delete(1, answers[0]['id'])
        assert bot.store.notes(1, query='Ответ номер 0') == []
        exported = bot.store.export_notes(1, folder)
        assert answers[0]['id'] not in {n['id'] for n in exported}
        assert 'ai_response' not in next(n for n in exported if n['id'] == voices[0]['id'])
    asyncio.run(run())
