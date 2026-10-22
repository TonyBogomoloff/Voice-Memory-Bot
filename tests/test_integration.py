import asyncio
import zipfile

from test_ai import bot as bot
from test_ai import ready_note, receive, step, tool
from test_mvp import store as store
from test_photos import photo_bot, receive_photo


def test_edited_voice_move_preserves_original_and_blocks_concurrent_ai_edit(bot, tmp_path):
    async def run():
        source = bot.store.create_folder(1, "Проект", 100)
        target = bot.store.create_folder(1, "Архив", 200)
        note = await receive(bot, voice=True, thread=100)
        request = ready_note(bot, "Исправь заметку", message_id=500)
        bot.ai_state.enqueue(1, request["id"], "button")
        tool(bot, request["id"], step("read_note", note_id=note["id"]))
        command = step("update_note", note_id=note["id"], revision=0,
                       title="Новый план", content="Сделать большие кнопки")
        assert tool(bot, request["id"], command)["ok"]
        bot.flush_ai_archive()
        edited = bot.store.note(1, note["id"])
        source_ids = bot.messages.in_thread(1, note["id"], 100)
        entered, release = asyncio.Event(), asyncio.Event()
        publish = bot.publish_note
        async def paused_publish(*args):
            entered.set()
            await release.wait()
            return await publish(*args)
        bot.publish_note = paused_publish
        bot.telegram.sent.clear()
        move = asyncio.create_task(bot.move(1, note["id"], target["id"], 100))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            tool(bot, request["id"], step("read_note", note_id=note["id"]))
            blocked = tool(bot, request["id"], {**command, "revision": 1, "content": "Гонка"})
            assert "error" in blocked
            assert bot.store.note(1, note["id"])["content"] == edited["content"]
        finally:
            release.set()
            await move
        sent = [p for m, p in bot.telegram.sent if m == "sendMessage"]
        assert sent[0]["text"] == edited["content"] and sent[0]["reply_to"] is None
        assert [p["message_id"] for m, p in bot.telegram.sent if m == "deleteMessage"] == source_ids
        saved = bot.store.note(1, note["id"])
        assert saved["folder_id"] == target["id"] and saved["transcript"] == note["transcript"]
        assert (bot.archive.directory(saved) / "transcript.txt").read_text() == note["transcript"]
        paths = bot.archive.export([saved], bot.store.folders(1), tmp_path / "export", False,
                                   100000, text_format="markdown")
        with zipfile.ZipFile(paths[0]) as archive:
            text = archive.read(next(n for n in archive.namelist() if n.endswith("history.md"))).decode()
            assert saved["content"] in text and saved["transcript"] in text
        assert source["id"] != saved["folder_id"]
    asyncio.run(run())


def test_edited_photo_caption_moves_in_full_and_audio_export_stays_empty(tmp_path, store):
    async def run():
        store.create_folder(1, "Игра", 100)
        target = store.create_folder(1, "Работа", 200)
        app = photo_bot(tmp_path, store)
        await receive_photo(app, "Исходная подпись")
        await app.process(store.claim_note())
        note = store.notes(1)[0]
        content = "Подробная подпись 🚀 " * 250
        request = ready_note(app, "Исправь подпись", message_id=500)
        app.ai_state.enqueue(1, request["id"], "button")
        tool(app, request["id"], step("read_note", note_id=note["id"]))
        assert tool(app, request["id"], step("update_note", note_id=note["id"], revision=0,
                    title="Фото", content=content))["ok"]
        app.telegram.sent.clear()
        await app.move(1, note["id"], target["id"], 100)
        assert app.telegram.sent[0][0] == "sendPhoto"
        assert app.telegram.sent[0][1]["caption"] is None
        texts = [p["text"] for m, p in app.telegram.sent if m == "sendMessage"]
        assert "".join(texts) == content
        assert store.note(1, note["id"])["transcript"] == "Исходная подпись"
        app.telegram.sent.clear()
        await app.send_export(1, 200, target["id"], "voice")
        assert len(app.telegram.sent) == 1 and "нет аудиозаписей" in app.telegram.sent[0][1]["text"]
    asyncio.run(run())
