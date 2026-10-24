import asyncio

import pytest

from test_ai import bot, receive, ready_note, step  # noqa: F401


@pytest.mark.parametrize('voice', [False, True])
def test_reply_to_saved_note_starts_ai_with_context(bot, voice):
    async def run():
        source = ready_note(bot)
        bot.messages.record(source, 1, None, 'source')
        note = await receive(bot, text='Объясни', reply=1, voice=voice)
        request = bot.ai_state.claim()
        assert request['trigger'] == 'reply'
        assert request['reply_context'] == source['transcript']
        await bot.ai_engine.run(request)
        payload = bot.assistant.calls[-1][0]
        assert payload['request'] == note['transcript']
        assert payload['reply_context'] == source['transcript']
    asyncio.run(run())


def test_photo_reply_passes_image_without_archive_permission(bot):
    async def run():
        bot.ai_state.set_settings(1, notes_access='none')
        from test_photos import PHOTO
        async def download(file_id, path, maximum):
            import hashlib
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PHOTO)
            return len(PHOTO), hashlib.sha256(PHOTO).hexdigest()
        bot.telegram.download = download
        calls = []
        async def respond(payload, model, images=()):
            calls.append((payload, images))
            return step()
        bot.assistant.respond = respond
        await bot.handle({'message': {'message_id': 101, 'from': {'id': 1},
            'chat': {'id': 1, 'type': 'private'},
            'reply_to_message': {'message_id': 50, 'text': 'Какой это цвет?'},
            'photo': [{'file_id': 'photo', 'file_size': len(PHOTO)}]}})
        note = bot.store.claim_note()
        await bot.process(note)
        request = bot.ai_state.claim()
        assert request is not None
        await bot.ai_engine.run(request)
        assert calls[0][0]['reply_context'] == 'Какой это цвет?'
        assert calls[0][1] == [bot.archive.audio(note)]
        assert calls[0][1][0].read_bytes() == PHOTO
    asyncio.run(run())


def test_topic_service_reply_is_not_ai_and_context_survives_recovery(bot):
    async def run():
        folder = bot.store.ensure_user(1)
        message = {'message_id': 101, 'reply_to_message': {
            'message_id': 50, 'forum_topic_created': {'name': 'Тема'}}}
        note, _ = bot.store.add_note(1, folder, message, 'text', transcript='Заметка')
        bot.capture_ai_input(note, message)
        bot.capture_ai_input(note, {'message_id': 101, 'reply_to_message': {'message_id': 50}})
        assert bot.ai_state.request(1, note['id']) is None
        message = {'message_id': 102, 'reply_to_message': {'message_id': 51, 'text': 'Контекст'}}
        note, _ = bot.store.add_note(1, folder, message, 'text', transcript='Вопрос')
        bot.capture_ai_input(note, {'message_id': 102})
        request = bot.ai_state.request(1, note['id'])
        assert request['reply_context'] == 'Контекст'
        bot.capture_ai_input(note, message)
        assert len(bot.store.all('SELECT * FROM ai_requests')) == 1
    asyncio.run(run())


def test_reply_capacity_applies_to_photo(bot):
    async def run():
        bot.store.limits = __import__('dataclasses').replace(bot.store.limits, max_pending_per_user=1)
        await receive(bot, reply=999)
        await bot.handle({'message': {'message_id': 102, 'from': {'id': 1},
            'chat': {'id': 1, 'type': 'private'},
            'reply_to_message': {'message_id': 50, 'text': 'Контекст'},
            'photo': [{'file_id': 'photo', 'file_size': 100}]}})
        assert bot.store.one('SELECT id FROM notes WHERE message_id=102') is None
        assert 'Очередь ИИ заполнена' in bot.telegram.sent[-1][1]['text']
    asyncio.run(run())
