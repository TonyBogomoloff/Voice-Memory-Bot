import asyncio

import pytest
from support.ai import bot as bot
from support.ai import ready_note


def test_cancelled_partial_delivery_keeps_receipts_and_never_replays(bot):
    async def run():
        note = ready_note(bot, 'Полный текст. ' * 500)
        bot.store.set_message_buttons(1, True)
        bot.store.update_note(1, note['id'], delivery='pending', transcript_delivery='pending')
        original = bot.telegram.text
        sent = []

        async def interrupted(*args, **kwargs):
            if sent:
                raise asyncio.CancelledError
            response = await original(*args, **kwargs)
            sent.append(response['message_id'])
            return response

        bot.telegram.text = interrupted
        with pytest.raises(asyncio.CancelledError):
            await bot.deliver(bot.store.note(1, note['id']))
        saved = bot.store.note(1, note['id'])
        assert saved['delivery'] == saved['transcript_delivery'] == 'unknown'
        assert sent[0] in {r['message_id'] for r in bot.messages.receipts(1, note['id'])}
        await bot.deliver(saved)
        assert len(sent) == 1
    asyncio.run(run())
