import asyncio
import zipfile

from support.ai import bot as bot
from support.ai import ready_note, receive, step, tool
from support.photos import photo_bot, receive_photo


def test_edited_voice_move_preserves_original_and_invalidates_stale_ai_edit(bot, tmp_path):
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
        tool(bot, request["id"], step("read_note", note_id=note["id"]))
        expected_ids = set(bot.messages.in_thread(1, note["id"], 100))
        bot.telegram.sent.clear()
        await bot.move(1, note["id"], target["id"], 100)
        blocked = tool(bot, request["id"], {**command, "revision": 1, "content": "Гонка"})
        assert "error" in blocked
        assert bot.store.note(1, note["id"])["content"] == edited["content"]
        assert all(method == "deleteMessage" for method, _ in bot.telegram.sent)
        assert {payload["message_id"] for _, payload in bot.telegram.sent} == expected_ids
        bot.flush_ai_archive()
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
        expected_ids = set(app.messages.in_thread(1, note["id"], 100))
        app.telegram.sent.clear()
        await app.move(1, note["id"], target["id"], 100)
        assert all(method == "deleteMessage" for method, _ in app.telegram.sent)
        assert {payload["message_id"] for _, payload in app.telegram.sent} == expected_ids
        assert store.note(1, note["id"])["content"] == content
        assert store.note(1, note["id"])["transcript"] == "Исходная подпись"
        app.telegram.sent.clear()
        await app.send_export(1, 200, target["id"], "voice")
        assert len(app.telegram.sent) == 1 and "нет аудиозаписей" in app.telegram.sent[0][1]["text"]
    asyncio.run(run())
