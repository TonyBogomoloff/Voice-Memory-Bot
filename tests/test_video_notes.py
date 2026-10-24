import asyncio

from test_mvp import make_bot
from test_mvp import store as store

from voice_memory_bot.archive import media_label


def test_video_note_is_transcribed_archived_once_and_keeps_selected_folder(tmp_path, store):
    async def run():
        folder = store.create_folder(1, "Кружки")
        store.select(1, folder["id"])
        bot = make_bot(tmp_path, store)
        event = {"message": {"message_id": 99, "from": {"id": 1},
                 "chat": {"id": 1, "type": "private"},
                 "video_note": {"file_id": "circle-id", "file_size": 32044, "duration": 1, "length": 240}}}
        await bot.handle(event)
        note = store.claim_note()
        assert note["kind"] == "video_note" and note["extension"] == "mp4"
        assert note["file_id"] == "circle-id"
        await bot.process(note)
        await bot.handle(event)
        saved = store.note(1, note["id"])
        assert saved["state"] == "ready"
        assert saved["transcript"] == "Сохрани в проект Игра. Добавить новые кнопки."
        assert saved["folder_id"] == folder["id"]
        assert bot.archive.audio(saved).exists() and saved["audio_bytes"] > 0
        assert media_label(saved) == "Видеокружок"
        assert bot.groq.calls == 1 and len(store.notes(1)) == 1
        assert any(saved["transcript"] in payload.get("text", "") for _, payload in bot.telegram.sent)
    asyncio.run(run())
