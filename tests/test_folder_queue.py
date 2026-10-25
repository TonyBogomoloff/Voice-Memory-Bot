import asyncio

import pytest
from test_commands import bot as bot
from test_folder_view import deleted_ids


def event(identity, text, owner=1):
    return {'update_id': identity, 'message': {'message_id': 1000 + identity, 'text': text,
            'from': {'id': owner}, 'chat': {'id': owner, 'type': 'private'}}}


def test_queued_folder_burst_renders_only_final_choice_and_removes_skipped_taps(bot):
    async def run():
        for name in ('Работа', 'Дом'):
            bot.store.create_folder(1, name)
        events = [event(1, '📁 Работа'), event(2, '📁 Дом'), event(3, '📁 Работа')]
        bot.store.ingest(events, {1})
        assert await bot.dispatch_next()
        assert not await bot.dispatch_next()
        assert bot.store.folder(1, bot.store.current_folder(1))['name'] == 'Работа'
        assert sum(p.get('text') == 'Загружаю папку…' for m, p in bot.telegram.sent if m == 'sendMessage') == 1
        assert {1001, 1002} <= deleted_ids(bot) and 1003 not in deleted_ids(bot)
        assert not bot.store.all('SELECT * FROM updates')
    asyncio.run(run())


def test_note_between_folder_taps_is_saved_in_its_original_destination(bot):
    async def run():
        work = bot.store.create_folder(1, 'Работа')['id']
        home = bot.store.create_folder(1, 'Дом')['id']
        bot.store.ingest([event(1, '📁 Работа'), event(2, 'Важная заметка'), event(3, '📁 Дом')], {1})
        assert await bot.dispatch_next()
        assert bot.store.current_folder(1) == work
        assert len(bot.store.all('SELECT * FROM updates')) == 2
        assert await bot.dispatch_next()
        assert bot.store.notes(1)[0]['folder_id'] == work
        assert await bot.dispatch_next()
        assert bot.store.current_folder(1) == home
        assert bot.store.notes(1)[0]['folder_id'] == work
    asyncio.run(run())


@pytest.mark.parametrize('barrier', ['command', 'other_owner', 'stale_button', 'callback'])
def test_coalescing_stops_at_non_equivalent_events(bot, barrier):
    bot.store.create_folder(1, 'Работа')
    middle = {'command': event(2, '/help'), 'other_owner': event(2, '📁 Работа', owner=2),
              'stale_button': event(2, '📁 Удалённая'),
              'callback': {'update_id': 2, 'callback_query': {'from': {'id': 1}, 'data': 'notes',
                           'message': {'chat': {'id': 1, 'type': 'private'}}}}}[barrier]
    bot.store.ingest([event(1, '📁 Работа'), middle, event(3, '📁 Входящие')], {1, 2})
    selected = bot.coalesce_folder_updates(bot.store.pending_update())
    assert [item['update_id'] for item in selected] == [1]
    assert bot.store.one('SELECT state FROM updates WHERE id=3')['state'] == 'pending'


def test_coalesced_noop_cleans_commands_and_recognizes_themed_labels(bot):
    async def run():
        bot.store.create_folder(1, 'Работа')
        bot.emoji_keyboards.remember(1, {'Работа': '📁 Работа', 'Входящие': '✅ Входящие'})
        bot.store.ingest([event(1, 'Работа'), event(2, 'Входящие')], {1})
        await bot.dispatch_next()
        assert not any(m == 'sendMessage' for m, _ in bot.telegram.sent)
        assert {1001, 1002} <= deleted_ids(bot)
        assert bot.store.folder(1, bot.store.current_folder(1))['name'] == 'Входящие'
    asyncio.run(run())


@pytest.mark.parametrize('cancelled', [False, True])
def test_failed_or_interrupted_burst_keeps_every_durable_event(bot, cancelled):
    async def run():
        bot.store.create_folder(1, 'Работа')
        bot.store.ingest([event(1, '📁 Работа'), event(2, '📁 Входящие')], {1})
        async def fail(update):
            if cancelled:
                raise asyncio.CancelledError
            raise RuntimeError('storage unavailable')
        bot.handle = fail
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await bot.dispatch_next()
        else:
            await bot.dispatch_next()
        rows = bot.store.all('SELECT id,state FROM updates ORDER BY id')
        assert [row['id'] for row in rows] == [1, 2]
        assert {row['state'] for row in rows} == ({'processing'} if cancelled else {'failed'})
    asyncio.run(run())
