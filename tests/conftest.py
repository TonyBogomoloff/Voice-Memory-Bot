import pytest

from voice_memory_bot.config import Limits
from voice_memory_bot.store import Store


@pytest.fixture
def store(tmp_path):
    database = Store(tmp_path / "archive.sqlite3", Limits())
    database.ensure_user(1)
    database.ensure_user(2)
    yield database
    database.close()
