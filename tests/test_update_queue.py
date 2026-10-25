import asyncio

import pytest
from test_mvp import make_bot
from test_mvp import store as store


def event(identity):
    return {'update_id': identity, 'message': {'message_id': identity, 'from': {'id': 1},
            'chat': {'id': 1, 'type': 'private'}, 'text': 'Сохранить'}}


def test_failed_updates_preserve_payloads_without_blocking_poll(store, tmp_path):
    async def run():
        with store.db:
            store.db.executemany("INSERT INTO updates VALUES (?,?,'failed')",
                                 ((i, '{"recoverable":true}') for i in range(1000)))
        store.ingest([event(i) for i in range(1000, 1999)], {1})
        app = make_bot(tmp_path, store)
        calls = []

        async def poll(method, **kwargs):
            calls.append((method, kwargs))
            raise asyncio.CancelledError

        app.telegram.call = poll
        with pytest.raises(asyncio.CancelledError):
            await app.poll()
        assert calls[0][0] == 'getUpdates' and calls[0][1]['limit'] == 1
        assert store.updates.counts() == {'pending': 999, 'processing': 0, 'failed': 1000}
        assert store.pending_update() == event(1000)
        store.fail_update(1000)
        assert store.updates.available() == 2
        assert store.one('SELECT payload FROM updates WHERE id=0')['payload'] == '{"recoverable":true}'
        store.finish_update(1001)
        assert store.updates.available() == 3
    asyncio.run(run())
