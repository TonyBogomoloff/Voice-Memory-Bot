import asyncio
import json

from test_commands import bot as bot, receive


def edit(message_id=101, text="Новый текст", owner=1, date=2):
    return {"update_id": date, "edited_message": {
        "message_id": message_id, "from": {"id": owner},
        "chat": {"id": owner, "type": "private"}, "text": text, "edit_date": date}}


def test_edit_queue_updates_existing_note_without_running_commands(bot):
    async def run():
        await receive(bot, "Старый текст", 101)
        note = bot.store.notes(1)[0]
        bot.store.set_action(1, "create")
        update = edit(text="/delete_all")
        bot.store.ingest([update], {1})
        assert bot.store.pending_update() == update
        bot.telegram.sent.clear()
        await bot.handle(update)
        current = bot.store.note(1, note["id"])
        assert current["transcript"] == "/delete_all"
        assert current["archive_dirty"] == 1
        assert bot.store.action(1) == "create"
        assert not bot.telegram.sent
        bot.flush_ai_archive()
        directory = bot.archive.directory(current)
        assert (directory / "transcript.txt").read_text() == "/delete_all"
        assert json.loads((directory / "source.json").read_text())["text"] == "/delete_all"
        await bot.handle(update)
        assert bot.store.note(1, note["id"])["revision"] == current["revision"]
        await bot.handle(edit(text="Устаревшее", date=1))
        assert bot.store.note(1, note["id"])["transcript"] == "/delete_all"
        await bot.handle(edit(message_id=999))
        await bot.handle(edit(owner=2))
        assert len(bot.store.notes(1)) == 1
        bot.store.delete(1, note["id"])
        await bot.handle(edit())
        assert bot.store.notes(1) == []
    asyncio.run(run())


def test_caption_clear_and_audio_transcript_preserved(bot):
    folder = bot.store.folders(1)[0]["id"]
    for mid, kind, transcript in [(201, "photo", "Подпись"), (202, "audio", "Расшифровка")]:
        message = {"message_id": mid, "caption": "Подпись"}
        note, _ = bot.store.add_note(1, folder, message, kind, transcript=transcript)
        update = edit(message_id=mid)
        update["edited_message"].pop("text")
        asyncio.run(bot.handle(update))
        current = bot.store.note(1, note["id"])
        assert "caption" not in json.loads(current["source_json"])
        assert current["transcript"] == ("" if kind == "photo" else transcript)
