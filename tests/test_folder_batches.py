import asyncio

import pytest
from support.commands import bot as bot
from support.folders import saved

from voice_memory_bot.providers import ProviderError, TelegramError


def test_batches_bound_requests_and_protect_sources_anchors_and_other_users(bot):
    async def run():
        expected = set(range(1000, 1205))
        for mid in expected | {8002, 8003}:
            bot.cleanup.remember(1, mid)
        folder = bot.store.current_folder(1)
        bot.store.add_note(1, folder, {'message_id': 8001}, 'voice', 'file', 'ogg', 12, 1)
        bot.cleanup.remember(1, 8001)
        bot.cleanup.remember(2, 9001)
        with bot.store.db:
            bot.store.db.execute('UPDATE telegram_messages SET view_blocked=1 WHERE message_id=8003')
        await bot.clear_folder_view(1, None, all_threads=True, keep_ids=(8002,))
        batches = [p['message_ids'] for m, p in bot.telegram.sent if m == 'deleteMessages']
        assert [len(b) for b in batches] == [100, 100, 5]
        assert {mid for b in batches for mid in b} == expected
        assert {r['message_id'] for r in bot.store.all('SELECT * FROM telegram_messages')} == {8001, 8002, 8003, 9001}
    asyncio.run(run())


@pytest.mark.parametrize('network_failure', [False, True])
def test_batch_failure_preserves_retry_receipts_and_isolates_forbidden(bot, network_failure):
    async def run():
        folder = bot.store.current_folder(1)
        for mid in (1001, 1002, 1003):
            saved(bot, folder, 'Запись', mid)
        original = bot.telegram.call
        async def call(method, **payload):
            if method == 'deleteMessages':
                if network_failure:
                    raise ProviderError('telegram_transport_unknown')
                raise TelegramError(400, method, "message can't be deleted")
            if payload.get('message_id') == 1002:
                raise TelegramError(400, method, "message can't be deleted")
            return await original(method, **payload)
        bot.telegram.call = call
        if network_failure:
            with pytest.raises(ProviderError):
                await bot.clear_folder_view(1, None, all_threads=True)
            assert len(bot.store.all('SELECT * FROM telegram_messages')) == 3
            bot.telegram.call = original
            await bot.clear_folder_view(1, None, all_threads=True)
            assert not bot.store.all('SELECT * FROM telegram_messages')
        else:
            assert await bot.clear_folder_view(1, None, all_threads=True) == 1
            remaining = bot.store.all('SELECT * FROM telegram_messages')
            assert len(remaining) == 1 and remaining[0]['message_id'] == 1002 and remaining[0]['view_blocked']
        assert len(bot.store.notes(1)) == 3
    asyncio.run(run())
