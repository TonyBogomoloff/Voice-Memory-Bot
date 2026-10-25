import hashlib

from support.base import make_bot

PHOTO = b"\xff\xd8\xff\xe0original photo bytes\xff\xd9"


async def receive_photo(bot, caption=""):
    await bot.handle({"message": {"message_id": 10, "from": {"id": 1},
        "chat": {"id": 1, "type": "private"}, "message_thread_id": 100, "caption": caption,
        "photo": [{"file_id": "small", "width": 90, "height": 90, "file_size": 12},
                  {"file_id": "photo-original", "width": 1280, "height": 1280, "file_size": len(PHOTO)}]}})


def photo_bot(tmp_path, store):
    bot = make_bot(tmp_path, store)
    bot.telegram.message_sequence = 1000
    async def download(file_id, destination, max_bytes):
        assert file_id == "photo-original"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(PHOTO)
        return len(PHOTO), hashlib.sha256(PHOTO).hexdigest()
    async def no_audio_or_llm(*args):
        raise AssertionError("Photos must not use audio processing or LLM routing")
    bot.telegram.download = download
    bot.groq.transcribe = bot.router.classify = bot.media.probe_duration = no_audio_or_llm
    return bot
