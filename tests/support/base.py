import hashlib
import wave

from voice_memory_bot.bot import Bot
from voice_memory_bot.config import Config
from voice_memory_bot.providers import ProviderError


def add(store, owner=1, message=10, **kwargs):
    folder = store.ensure_user(owner)
    return store.add_note(owner, folder, {"message_id": message}, **kwargs)[0]


def route_result(folder, **overrides):
    return {"title": "Идея интерфейса", "explicit": True, "ambiguous": False,
            "folder_id": folder["id"], "requested_folder": folder["name"],
            "evidence": f"Сохрани в проект {folder['name']}", **overrides}


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.fail_delivery = False
        self.message_sequence = 0

    async def call(self, method, **kwargs):
        self.sent.append((method, kwargs))
        if method == "createForumTopic":
            return {"message_thread_id": 123}
        if method.startswith("editMessage"):
            return {"message_id": kwargs["message_id"]}
        if self.fail_delivery and method.startswith("send"):
            raise ProviderError("telegram_transport_unknown")
        self.message_sequence += 1
        result = {"message_id": self.message_sequence}
        if getattr(self, "on_message", None) and method.startswith("send"):
            self.on_message(kwargs["chat_id"], result["message_id"],
                            kwargs.get("message_thread_id", kwargs.get("thread")))
        return result

    async def text(self, owner, text, thread=None, keyboard=None, reply_to=None):
        return await self.call("sendMessage", chat_id=owner, text=text, thread=thread, keyboard=keyboard,
                               reply_to=reply_to)

    async def photo(self, owner, path, thread=None, caption=None, keyboard=None, reply_to=None, edit_id=None):
        result = await self.call("editMessageMedia" if edit_id else "sendPhoto", chat_id=owner,
            path=str(path), image=path.read_bytes(), message_id=edit_id, message_thread_id=thread,
            caption=caption, reply_markup=keyboard, reply_to=reply_to)
        return {**result, "photo": [{"file_id": "generated-photo", "width": 1024, "height": 1024}]}

    async def video(self, owner, path, thread=None, caption=None, keyboard=None, *, duration=10, width=720, height=1280, cover=None):
        return await self.call("sendVideo", chat_id=owner, path=str(path), caption=caption,
                               thread=thread, keyboard=keyboard, duration=duration, width=width, height=height,
                               cover=str(cover) if cover else None)

    async def help_media(self, owner, path, thread=None, caption=None, keyboard=None, *, edit_id=None):
        method = "editMessageMedia" if edit_id else ("sendVideo" if path.suffix == ".mp4" else "sendPhoto")
        if self.fail_delivery and edit_id:
            self.sent.append((method, {"message_id": edit_id}))
            raise ProviderError("telegram_transport_unknown")
        return await self.call(method, chat_id=owner, path=str(path), caption=caption,
                               thread=thread, keyboard=keyboard, message_id=edit_id)

    async def download(self, file_id, destination, max_bytes):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 16000)
        data = destination.read_bytes()
        return len(data), hashlib.sha256(data).hexdigest()


class FakeGroq:
    calls = 0
    error = None

    async def transcribe(self, path, *, prompt=None):
        self.calls += 1
        if self.error:
            raise ProviderError(self.error)
        return "Сохрани в проект Игра. Добавить новые кнопки."


class FakeRouter:
    error = False

    async def folder_name(self, transcript):
        raise ProviderError("fake_folder_name_disabled")

    async def classify(self, transcript, folders):
        if self.error:
            raise ProviderError("codex_timeout")
        return route_result(next(f for f in folders if f["name"] == "Игра"))


def make_bot(tmp_path, store):
    config = Config("123:secret", frozenset({1}), "secret", tmp_path)
    app = Bot(config, store, FakeTelegram(), FakeGroq(), FakeRouter())
    app.folder_namer = app.router
    return app


async def receive_voice(bot, message_id=10, thread=100):
    await bot.handle({"message": {"message_id": message_id, "from": {"id": 1},
        "chat": {"id": 1, "type": "private"}, "message_thread_id": thread,
        "voice": {"file_id": "voice-id", "file_size": 32044, "duration": 1}}})
