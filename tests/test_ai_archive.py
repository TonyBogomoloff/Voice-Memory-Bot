import asyncio
import zipfile
from dataclasses import replace

import pytest
from support.ai import bot as bot
from support.ai import ready_note, step

from voice_memory_bot.ai_store import AIStore
from voice_memory_bot.store import Store


@pytest.mark.parametrize("trigger", ["folder", "reply", "name", "button"])
def test_every_ai_reply_is_archived_once_before_delivery_and_searchable(bot, trigger, tmp_path):
    async def run():
        source = ready_note(bot, "Составь план")
        original = source["transcript"]
        bot.ai_state.enqueue(1, source["id"], trigger)
        other = bot.store.create_folder(1, "Другая папка")
        bot.store.select(1, other["id"])
        bot.ai_state.set_settings(1, notes_access="none")
        text = "План с уникальным словом: астролябия.\n" * 250
        bot.assistant.responses = [step(message=text)]
        bot.telegram.fail_delivery = True
        await bot.process_ai(bot.ai_state.claim())
        request = bot.ai_state.request(1, source["id"])
        answer = bot.store.note(1, request["result_note_id"])
        assert answer["folder_id"] == source["folder_id"]
        assert answer["transcript"] == text and answer["ai_generated_from"] == source["id"]
        assert bot.store.note(1, source["id"])["transcript"] == original
        assert (bot.archive.directory(answer) / "transcript.txt").read_text() == text
        assert text in (bot.archive.directory(answer) / "note.md").read_text()
        assert bot.store.notes(1, query="астролябия")[0]["id"] == answer["id"]
        assert not bot.store.notes(2, query="астролябия")
        assert request["delivery"] == "unknown"
        bot.telegram.fail_delivery = False
        await bot.ai_engine.run(request)  # Replay a saved model result after an interrupted job.
        await bot.deliver_ai(request, force=True)
        assert bot.ai_state.request(1, source["id"])["result_note_id"] == answer["id"]
        assert len(bot.store.all("SELECT id FROM notes WHERE ai_generated_from=?", (source["id"],))) == 1
        assert bot.messages.in_thread(1, answer["id"], None)
        paths = bot.archive.export(bot.store.export_notes(1), bot.store.folders(1), tmp_path / "export",
                                   False, 45_000_000, text_format="markdown")
        with zipfile.ZipFile(paths[0]) as archive:
            document = "".join(archive.read(p).decode() for p in archive.namelist() if p.endswith("history.md"))
            assert document.count(text) == 1
    asyncio.run(run())


def test_backfill_preserves_full_history_and_delivery_without_resurrecting_deleted_answers(bot, tmp_path):
    source = ready_note(bot, "Старый вопрос")
    deleted = ready_note(bot, "Удалённый вопрос", message_id=2)
    failed = ready_note(bot, "Неотвеченный вопрос", message_id=3)
    for note in (source, deleted, failed):
        bot.ai_state.enqueue(1, note["id"], "button")
    text = "Старый полный ответ. " * 3000
    bot.ai_state.update_request(1, source["id"], state="ready", response=text, delivery="unknown")
    bot.ai_state.update_request(1, deleted["id"], state="ready", response="Удалено", delivery="sent")
    bot.store.delete(1, deleted["id"])
    bot.ai_state.update_request(1, failed["id"], state="failed", response="Ошибка провайдера", delivery="sent")
    request = bot.ai_state.request(1, source["id"])
    limits = replace(bot.store.limits, max_notes_per_user=1, max_pending_per_user=1)
    reopened = Store(tmp_path / "db", limits)
    try:
        state = AIStore(reopened)
        state.archive_replies()
        first = state.request(1, source["id"])
        answer = reopened.note(1, first["result_note_id"])
        assert answer["transcript"] == text and answer["created_at"] == request["created_at"]
        assert first["delivery"] == "unknown" and first["response"] == text
        state.archive_replies()
        assert len(reopened.notes(1)) == 1
        assert state.request(1, failed["id"])["result_note_id"] is None
        reopened.delete(1, answer["id"])
        state.archive_replies()
        assert reopened.notes(1) == []
        assert state.request(1, source["id"])["result_note_id"] == answer["id"]
    finally:
        reopened.close()


def test_reply_and_result_note_commit_atomically(bot, monkeypatch):
    from voice_memory_bot import ai_store
    source = ready_note(bot)
    bot.ai_state.enqueue(1, source["id"], "button")
    create = ai_store.result_text

    def interrupted(*args, **kwargs):
        create(*args, **kwargs)
        raise OSError("storage interruption")

    monkeypatch.setattr(ai_store, "result_text", interrupted)
    with pytest.raises(OSError):
        bot.ai_state.finish_reply(1, source["id"], "Готовый ответ")
    assert bot.store.all("SELECT id FROM notes WHERE ai_generated_from=?", (source["id"],)) == []
    request = bot.ai_state.request(1, source["id"])
    assert request["response"] is None and request["result_note_id"] is None
    monkeypatch.setattr(ai_store, "result_text", create)
    bot.store.limits = replace(bot.store.limits, max_notes_per_user=1)
    bot.ai_state.finish_reply(1, source["id"], "Готовый ответ")
    assert bot.ai_state.request(1, source["id"])["result_note_id"]


def test_saved_reply_survives_folder_switch_and_can_be_moved_and_continued(bot):
    async def run():
        bot.store.set_message_buttons(1, True)
        source = ready_note(bot, "Вопрос в первой папке")
        bot.ai_state.enqueue(1, source["id"], "button")
        bot.assistant.responses = [step(message="Сохранённый ответ")]
        await bot.process_ai(bot.ai_state.claim())
        request = bot.ai_state.request(1, source["id"])
        answer = bot.store.note(1, request["result_note_id"])
        other = bot.store.create_folder(1, "Другая")
        await bot.show_notes(1, None, other["id"])
        bot.telegram.sent.clear()
        await bot.show_notes(1, None, source["folder_id"])
        replies = [p for method, p in bot.telegram.sent
                   if method == "sendMessage" and p.get("text") == "🤖 Сохранённый ответ"]
        assert len(replies) == 1
        buttons = [b["callback_data"] for row in replies[0]["keyboard"]["inline_keyboard"] for b in row]
        assert f"pick:{answer['id']}:0" in buttons
        assert not any(button.startswith("aicontinue:") for button in buttons)
        ids = bot.messages.in_thread(1, answer["id"], None)
        assert bot.ai_state.reply_conversation(1, ids[-1]) == request["conversation_id"]
        await bot.show_note(1, answer["id"], None)
        reopened = bot.messages.in_thread(1, answer["id"], None)[-1]
        assert bot.ai_state.reply_conversation(1, reopened) == request["conversation_id"]
        await bot.move(1, answer["id"], other["id"], None)
        assert bot.store.note(1, answer["id"])["folder_id"] == other["id"]
        assert bot.store.note(1, source["id"])["folder_id"] == source["folder_id"]
        assert bot.ai_state.request(1, source["id"])["response"] == "Сохранённый ответ"
    asyncio.run(run())
