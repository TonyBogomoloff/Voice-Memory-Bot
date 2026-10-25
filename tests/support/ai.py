import pytest

from support.base import FakeGroq, FakeTelegram
from voice_memory_bot.bot import Bot
from voice_memory_bot.config import Config, Limits
from voice_memory_bot.store import Store


def step(action="reply", **kwargs):
    return {"action": action, "message": "Ответ", "query": "", "note_id": "", "folder_id": "",
            "title": "", "content": "", "revision": -1, **kwargs}


class Assistant:
    def __init__(self):
        self.calls, self.address_calls = [], []
        self.responses = []

    async def respond(self, payload, model):
        self.calls.append((payload, model))
        result = self.responses.pop(0) if self.responses else step()
        if isinstance(result, Exception):
            raise result
        return result

    async def addressed(self, text, names, model):
        self.address_calls.append(text)
        return text.startswith("Луна,") or text.endswith(", Луна?")


class Router:
    calls = 0

    async def classify(self, transcript, folders):
        self.calls += 1
        return {"title": "Заметка", "explicit": False, "ambiguous": False,
                "folder_id": None, "requested_folder": None, "evidence": None}


@pytest.fixture
def bot(tmp_path):
    store = Store(tmp_path / "db", Limits())
    store.ensure_user(1)
    store.ensure_user(2)
    config = Config("1:token", frozenset({1, 2}), "key", tmp_path, assistant_models=("another-model",))
    app = Bot(config, store, FakeTelegram(), FakeGroq(), Router(), Assistant())
    yield app
    store.close()


async def receive(bot, text="Привет", message_id=101, thread=None, reply=None, voice=False):
    message = {"message_id": message_id, "from": {"id": 1}, "chat": {"id": 1, "type": "private"}}
    if thread:
        message["message_thread_id"] = thread
    if reply:
        message["reply_to_message"] = {"message_id": reply}
    if voice:
        message["voice"] = {"file_id": "voice", "file_size": 32044, "duration": 1}
    else:
        message["text"] = text
    await bot.handle({"message": message})
    note = bot.store.one("SELECT * FROM notes WHERE owner=1 AND message_id=?", (message_id,))
    await bot.process(bot.store.claim_note())
    return bot.store.note(1, note["id"])


def ready_note(bot, text="Старая мысль", owner=1, message_id=1):
    folder = bot.store.ensure_user(owner)
    note, _ = bot.store.add_note(owner, folder, {"message_id": message_id}, "text", transcript=text)
    bot.store.update_note(owner, note["id"], state="ready", title="План", routing="manual", delivery="sent")
    return bot.store.note(owner, note["id"])


def tool(bot, request_id, command):
    index = len(bot.ai_state.steps(1, request_id))
    bot.ai_state.save_command(1, request_id, index, command)
    return bot.ai_state.execute(1, request_id, index)
