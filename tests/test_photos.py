import asyncio
import hashlib
import zipfile

import pytest
from test_mvp import make_bot
from test_mvp import store as store

from voice_memory_bot.providers import ProviderError

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
    bot.groq.transcribe = bot.router.classify = bot.probe_duration = no_audio_or_llm
    return bot


@pytest.mark.parametrize("caption", ["", "Сохрани в проект Работа. Подпись к снимку."])
def test_photo_controls_archive_and_move_remove_only_source_messages(tmp_path, store, caption):
    async def run():
        game = store.create_folder(1, "Игра", thread=100)
        work = store.create_folder(1, "Работа", thread=200)
        bot = photo_bot(tmp_path, store)
        await receive_photo(bot, caption)
        note = store.notes(1)[0]
        assert len(bot.telegram.sent) == 1
        method, controls = bot.telegram.sent[0]
        assert method == "sendMessage" and controls["thread"] == 100
        assert controls["keyboard"]["inline_keyboard"] == [[{
            "text": "✍️ Записать сюда", "callback_data": "record:100"}, {
            "text": "Опции", "callback_data": f"options:{note['id']}"}]]
        await bot.process(store.claim_note())
        note = store.note(1, note["id"])
        assert note["state"] == "ready" and note["delivery"] == "sent"
        assert note["folder_id"] == game["id"] and note["transcript"] == caption
        assert len(bot.telegram.sent) == 1  # No extra copy or caption in the source topic.
        assert bot.archive.audio(note).name == "photo.jpg"
        assert bot.archive.audio(note).read_bytes() == PHOTO
        paths = bot.archive.export([note], store.folders(1), tmp_path / "export", True, 100000,
                                   text_format="markdown")
        with zipfile.ZipFile(paths[0]) as archive:
            assert archive.read(next(n for n in archive.namelist() if n.endswith(".jpg"))) == PHOTO
            markdown = archive.read(next(n for n in archive.namelist() if n.endswith("history.md"))).decode()
            assert "[Фото](photos/" in markdown and caption in markdown
        assert bot.archive.export([note], store.folders(1), tmp_path / "audio-only", True,
                                  100000, audio_only=True) == []
        control_id = note["transcript_message_id"]
        await bot.callback(1, 100, control_id, f"options:{note['id']}")
        assert bot.telegram.sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Переместить"
        await bot.callback(1, 100, control_id, f"pick:{note['id']}:0")
        bot.telegram.sent.clear()
        notification = await bot.callback(1, 100, control_id, f"place:{note['id']}:{work['id']}")
        sent_photo = bot.telegram.sent[0]
        assert sent_photo[0] == "sendPhoto"
        assert sent_photo[1]["photo"] == "photo-original" and sent_photo[1]["message_thread_id"] == 200
        assert sent_photo[1]["caption"] == (caption or None)
        assert sent_photo[1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Опции"
        assert [p["message_id"] for m, p in bot.telegram.sent if m == "deleteMessage"] == [10, control_id]
        assert notification == "Запись перенесена."
        assert all(m != "sendMessage" for m, _ in bot.telegram.sent)
        destination_ids = bot.messages.in_thread(1, note["id"], 200)
        assert len(destination_ids) == 1
        bot.telegram.sent.clear()
        await bot.move(1, note["id"], game["id"], 200)
        assert bot.telegram.sent[0][0] == "sendPhoto"
        assert [p["message_id"] for m, p in bot.telegram.sent if m == "deleteMessage"] == destination_ids
    asyncio.run(run())


def test_photo_failed_download_can_retry_without_losing_controls(tmp_path, store):
    async def run():
        store.create_folder(1, "Игра", thread=100)
        bot = photo_bot(tmp_path, store)
        download = bot.telegram.download
        async def fail_download(*args):
            raise ProviderError("telegram_download_503")
        bot.telegram.download = fail_download
        await receive_photo(bot)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        assert note["state"] == "asr_failed"
        assert bot.telegram.sent[-1][0] == "editMessageText"
        assert "Не удалось сохранить фото" in bot.telegram.sent[-1][1]["text"]
        bot.telegram.download = download
        await bot.callback(1, 100, note["transcript_message_id"], f"retry:{note['id']}")
        await bot.process(store.claim_note())
        saved = store.note(1, note["id"])
        assert saved["state"] == "ready" and saved["delivery"] == "sent"
        assert saved["status_message_id"] is None
        assert bot.messages.in_thread(1, note["id"], 100) == [10, saved["transcript_message_id"]]
        assert bot.telegram.sent[-1][1]["keyboard"]["inline_keyboard"][0][-1]["text"] == "Опции"
    asyncio.run(run())


def test_photo_uncertain_move_keeps_original_and_controls(tmp_path, store):
    async def run():
        game = store.create_folder(1, "Игра", thread=100)
        work = store.create_folder(1, "Работа", thread=200)
        bot = photo_bot(tmp_path, store)
        await receive_photo(bot)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        call = bot.telegram.call
        async def fail_photo(method, **kwargs):
            if method == "sendPhoto":
                raise ProviderError("telegram_transport_unknown")
            return await call(method, **kwargs)
        bot.telegram.call = fail_photo
        bot.telegram.sent.clear()
        await bot.move(1, note["id"], work["id"], 100)
        assert all(m != "deleteMessage" for m, _ in bot.telegram.sent)
        assert store.note(1, note["id"])["folder_id"] == game["id"]
        assert bot.messages.in_thread(1, note["id"], 100) == [10, note["transcript_message_id"]]
    asyncio.run(run())
