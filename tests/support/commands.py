import pytest

from support.base import make_bot
from voice_memory_bot.config import Limits
from voice_memory_bot.store import Store


@pytest.fixture
def bot(tmp_path):
    store = Store(tmp_path / "archive.sqlite3", Limits())
    store.ensure_user(1)
    app = make_bot(tmp_path, store)
    yield app
    store.close()


async def receive(bot, text, message_id=1, thread=None):
    await bot.handle({"message": {"message_id": message_id, "from": {"id": 1},
                     "chat": {"id": 1, "type": "private"}, "message_thread_id": thread, "text": text}})
