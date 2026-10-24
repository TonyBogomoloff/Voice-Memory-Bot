import asyncio

import pytest
from test_commands import bot as bot
from test_commands import receive

from voice_memory_bot.providers import ProviderError


@pytest.mark.parametrize("text", ["  Точный текст\nс переносом  ", "Длинный текст " * 400])
def test_text_copy_is_immediate_standalone_and_deletes_original_after_all_chunks(bot, text):
    async def run():
        await receive(bot, text, 5001)
        sent = [p for m, p in bot.telegram.sent if m == "sendMessage"]
        assert ''.join(p['text'] for p in sent) == text
        assert all(p['reply_to'] is None for p in sent)
        assert sent[-1]['keyboard']['inline_keyboard'][0][0]['text'] == 'Опции'
        assert bot.telegram.sent[-1] == ('deleteMessage', {'chat_id': 1, 'message_id': 5001})
        note = bot.store.notes(1)[0]
        assert note['transcript'] == text and note['transcript_delivery'] == 'sent'
        bot.telegram.sent.clear()
        await bot.process(note)
        assert not any(m == 'sendMessage' for m, _ in bot.telegram.sent)
    asyncio.run(run())


@pytest.mark.parametrize('failed_method', ['sendMessage', 'deleteMessage'])
def test_text_copy_failure_keeps_original_and_does_not_duplicate_copy(bot, failed_method):
    async def run():
        original_call = bot.telegram.call

        async def fail(method, **payload):
            if method == failed_method:
                raise ProviderError('telegram_transport_unknown')
            return await original_call(method, **payload)

        bot.telegram.call = fail
        await receive(bot, 'Сохранить текст', 5001)
        note = bot.store.notes(1)[0]
        assert note['transcript_delivery'] == ('unknown' if failed_method == 'sendMessage' else 'sent')
        assert bot.store.one('SELECT * FROM telegram_messages WHERE message_id=5001')
        bot.telegram.call = original_call
        bot.telegram.sent.clear()
        await bot.process(note)
        assert not any(m == 'sendMessage' for m, _ in bot.telegram.sent)
    asyncio.run(run())
