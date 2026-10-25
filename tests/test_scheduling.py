import asyncio

import pytest
from test_ai import bot as bot
from test_ai import ready_note


def event(identity, owner):
    return {'update_id': identity, 'message': {'message_id': identity, 'text': 'Запись',
            'from': {'id': owner}, 'chat': {'id': owner, 'type': 'private'}}}


def test_slow_owner_does_not_block_another_and_shutdown_preserves_order(bot):
    async def run():
        entered, other_done = asyncio.Event(), asyncio.Event()
        bot.store.ingest([event(1, 1), event(2, 1), event(3, 2)], {1, 2})

        async def handle(update):
            if update['update_id'] == 1:
                entered.set()
                await asyncio.Event().wait()
            assert update['update_id'] == 3
            other_done.set()

        bot.handle = handle
        task = asyncio.create_task(bot.dispatch())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(other_done.wait(), 1)
            assert bot.store.one('SELECT state FROM updates WHERE id=2')['state'] == 'pending'
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert bot.store.one('SELECT state FROM updates WHERE id=1')['state'] == 'processing'
        assert bot.store.one('SELECT state FROM updates WHERE id=3') is None
        bot.store.recover_runtime()
        resumed = []

        async def complete(update):
            resumed.append(update['update_id'])

        bot.handle = complete
        assert await bot.dispatch_next() and await bot.dispatch_next()
        assert resumed == [1, 2]
    asyncio.run(run())


@pytest.mark.parametrize('worker', ['media', 'ai'])
@pytest.mark.parametrize('blocked', ['view', 'processing'])
def test_workers_only_pause_the_busy_owner(bot, worker, blocked):
    async def run():
        first = ready_note(bot, owner=1)
        second = ready_note(bot, owner=2)
        done = asyncio.Event()
        entered = asyncio.Event()
        if worker == 'media':
            for note in (first, second):
                bot.store.update_note(note['owner'], note['id'], state='queued')
        else:
            for note in (first, second):
                bot.ai_state.enqueue(note['owner'], note['id'], 'button')
        if blocked == 'view':
            bot.view_rendering.add(1)

        async def process(item):
            if item['owner'] == 1:
                entered.set()
                await asyncio.Event().wait()
            if worker == 'media':
                bot.store.update_note(2, item['id'], state='ready', delivery='sent')
            else:
                bot.ai_state.update_request(2, item['note_id'], state='ready', delivery='sent')
            done.set()

        if worker == 'media':
            bot.process = process
            task = asyncio.create_task(bot.worker())
        else:
            bot.process_ai = process
            task = asyncio.create_task(bot.ai_worker())
        try:
            await asyncio.wait_for(done.wait(), 1)
            assert entered.is_set() == (blocked == 'processing')
            if blocked == 'view':
                state = (bot.store.note(1, first['id']) if worker == 'media'
                         else bot.ai_state.request(1, first['id']))
                assert state['state'] == 'queued'
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(run())
