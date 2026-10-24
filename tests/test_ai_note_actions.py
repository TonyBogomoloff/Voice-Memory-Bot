import asyncio
import json

import pytest
from test_ai import bot as bot
from test_ai import ready_note, receive, step, tool

from voice_memory_bot.providers import ProviderError


def request_for(bot, note=None):
    request = ready_note(bot, "Перенеси или удали выбранную запись", message_id=901)
    conversation = bot.ai_state.start_note_conversation(1, note["id"]) if note else None
    bot.ai_state.enqueue(1, request["id"], "button", conversation=conversation)
    return request["id"]


async def act(bot, request, action, note, **fields):
    index = len(bot.ai_state.steps(1, request))
    bot.ai_state.save_command(1, request, index,
                              step(action, note_id=note["id"], revision=note["revision"], **fields))
    return await bot.ai_note_actions.execute(1, request, index)


def read(bot, request, note):
    return tool(bot, request, step("read_note", note_id=note["id"]))


def test_voice_move_then_delete_is_single_exact_and_durable(bot):
    async def run():
        note = await receive(bot, voice=True)
        other = ready_note(bot, message_id=888)
        original = bot.archive.audio(note).read_bytes()
        request = request_for(bot)
        folder = bot.store.create_folder(1, "Работа")
        read(bot, request, note)
        moved = await act(bot, request, "move_note", note, folder_id=folder["id"])
        assert moved["ok"] and moved["cleanup_state"] == "done"
        current = bot.store.note(1, note["id"])
        assert current["folder_id"] == folder["id"]
        assert bot.archive.audio(current).read_bytes() == original
        assert current["transcript"] == note["transcript"]
        read(bot, request, other)
        assert "только одну" in (await act(bot, request, "move_note", other, folder_id=folder["id"]))["error"]
        read(bot, request, current)
        bot.messages.record(current, 777, None, "view")
        deleted = await act(bot, request, "delete_note", current)
        assert deleted["ok"] and deleted["cleanup_state"] == "done"
        assert not bot.archive.audio(current).exists()
        assert not bot.store.one("SELECT id FROM notes WHERE id=? AND deleted=0", (note["id"],))
        assert "только одну" in (await act(bot, request, "delete_note", other))["error"]
        assert bot.store.note(1, other["id"])["transcript"] == other["transcript"]
        ids = [p["message_id"] for m, p in bot.telegram.sent if m == "deleteMessage"]
        assert 777 in ids and 888 not in ids
        assert bot.store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    asyncio.run(run())


@pytest.mark.parametrize("target", ["new", "existing", "cancel"])
def test_missing_folder_renders_buttons_and_choice_is_single_use(bot, target):
    async def run():
        note = ready_note(bot)
        folder = bot.store.create_folder(1, "Путешествия")
        bot.store.create_folder(1, "Работа")
        request = request_for(bot, note)
        bot.assistant.responses = [step("list_folders"), step("read_note", note_id=note["id"]),
            step("move_note", note_id=note["id"], revision=0, title="Путешествие")]
        await bot.process_ai(bot.ai_state.claim())
        assert bot.store.note(1, note["id"])["folder_id"] == note["folder_id"]
        buttons = bot.telegram.sent[-1][1]["keyboard"]["inline_keyboard"]
        labels = [b["text"] for row in buttons for b in row]
        assert labels[:2] == ["➕ Новая папка", "📁 Путешествия"]
        callback = next(b["callback_data"] for row in buttons for b in row
                        if b["callback_data"].endswith(":" + (folder["id"] if target == "existing" else target)))
        await bot.callback(1, None, 999, callback)
        count = len(bot.store.folders(1))
        current = bot.store.note(1, note["id"])
        assert count == (4 if target == "new" else 3)
        expected = ("Путешествие" if target == "new" else "Путешествия" if target == "existing" else "Входящие")
        assert bot.store.folder(1, current["folder_id"])["name"] == expected
        await bot.callback(1, None, 999, callback)
        assert bot.store.note(1, note["id"])["revision"] == current["revision"]
        assert len(bot.store.folders(1)) == count
        assert bot.ai_note_actions.buttons(1, request) == []
        assert bot.ai_state.request(1, request)["response"] != "Ответ"
        bot.ai_state.archive_replies()
        assert bot.ai_state.request(1, request)["result_note_id"] is None
    asyncio.run(run())


@pytest.mark.parametrize("change", ["revision", "access", "removed", "owner", "forged"])
def test_pending_choice_rechecks_target_owner_permission_and_revision(bot, change):
    async def run():
        note = ready_note(bot)
        folder = bot.store.create_folder(1, "Работа")
        request = request_for(bot)
        read(bot, request, note)
        await act(bot, request, "move_note", note, title="Работка")
        bot.ai_state.finish_reply(1, request, "Выбери папку")
        if change == "revision":
            with bot.store.db:
                bot.store.db.execute("UPDATE notes SET revision=revision+1 WHERE id=?", (note["id"],))
        elif change == "access":
            bot.ai_state.set_settings(1, notes_access="read")
        elif change == "removed":
            bot.store.delete_folder(1, folder["id"])
        target = "invalid" if change == "forged" else folder["id"]
        with pytest.raises(ValueError):
            await bot.ai_note_actions.choose(2 if change == "owner" else 1, request, 1, target)
        assert bot.store.note(1, note["id"])["folder_id"] == note["folder_id"]
        assert len(bot.store.folders(1, include_removed=True)) == 2
    asyncio.run(run())


def test_existing_alias_and_folder_created_while_waiting_are_reused(bot):
    async def run():
        note = ready_note(bot)
        request = request_for(bot)
        read(bot, request, note)
        await act(bot, request, "move_note", note, title="Проекты")
        folder = bot.store.create_folder(1, "Проекты")
        await bot.ai_note_actions.choose(1, request, 1, "new")
        assert len(bot.store.folders(1)) == 2
        assert bot.store.note(1, note["id"])["folder_id"] == folder["id"]
        with bot.store.db:
            bot.store.db.execute("UPDATE folders SET aliases=? WHERE id=?", (json.dumps(["Бизнес"]), folder["id"]))
        # A separate request can move another note; exact aliases require no clarification.
        request2 = ready_note(bot, "В бизнес", message_id=902)
        bot.ai_state.enqueue(1, request2["id"], "button")
        other = ready_note(bot, message_id=903)
        read(bot, request2["id"], other)
        result = await act(bot, request2["id"], "move_note", other, title="бизнес")
        assert result["ok"] and result["folder_id"] == folder["id"]
    asyncio.run(run())


@pytest.mark.parametrize("action", ["move_note", "delete_note"])
def test_action_requires_read_write_access_and_owner_and_cannot_bypass_scoped_note(bot, action):
    async def run():
        note = ready_note(bot)
        other = ready_note(bot, message_id=2)
        foreign = ready_note(bot, owner=2)
        folder = bot.store.create_folder(1, "Работа")
        request = request_for(bot, note)
        fields = {"folder_id": folder["id"]} if action == "move_note" else {}
        assert (await act(bot, request, action, note, **fields))["error"]
        read(bot, request, note)
        assert (await act(bot, request, action, other, **fields))["error"]
        assert (await act(bot, request, action, foreign, **fields))["error"]
        bot.ai_state.set_settings(1, notes_access="read")
        assert (await act(bot, request, action, note, **fields))["error"]
        bot.ai_state.set_settings(1, notes_access="write")
        async with bot.note_locks[note["id"]]:
            assert (await act(bot, request, action, note, **fields))["error"]
        assert (await act(bot, request, action, note, **fields))["ok"]
    asyncio.run(run())


def test_delete_selected_note_can_finish_reply_without_reading_deleted_context(bot):
    async def run():
        note = ready_note(bot)
        request = request_for(bot, note)
        bot.assistant.responses = [step("read_note", note_id=note["id"]),
                                  step("delete_note", note_id=note["id"], revision=0), step(message="Удалил.")]
        await bot.process_ai(bot.ai_state.claim())
        current = bot.ai_state.request(1, request)
        assert current["state"] == "ready" and current["delivery"] == "sent"
        assert bot.assistant.calls[-1][0]["selected_note"]["kind"] == "deleted"
        rows, changes = bot.ai_result_controls(1, request, current)
        assert changes == ["Удалено: План"]
        assert all(note["id"] not in b["callback_data"] for row in rows for b in row)
        assert bot.ai_state.note_context(1, current["conversation_id"])["content"] == ""
    asyncio.run(run())


@pytest.mark.parametrize("action", ["move_note", "delete_note"])
def test_receipt_commits_before_cleanup_and_replay_does_not_repeat_action(bot, action, monkeypatch):
    async def run():
        note = ready_note(bot)
        bot.messages.record(note, note["message_id"], None, "source")
        bot.archive.persist_text(note, bot.store.folder(1, note["folder_id"]))
        folder = bot.store.create_folder(1, "Работа")
        request = request_for(bot)
        read(bot, request, note)
        cleanup = bot.ai_note_actions.cleanup
        async def crash(*args):
            raise asyncio.CancelledError
        monkeypatch.setattr(bot.ai_note_actions, "cleanup", crash)
        with pytest.raises(asyncio.CancelledError):
            await act(bot, request, action, note, folder_id=folder["id"] if action == "move_note" else "")
        assert bot.ai_state.steps(1, request)[1]["result"]["ok"]
        assert bot.store.one("SELECT * FROM ai_note_jobs")["state"] == "pending"
        monkeypatch.setattr(bot.ai_note_actions, "cleanup", cleanup)
        bot.init_ai(bot.assistant)
        result = await bot.ai_note_actions.execute(1, request, 1)
        assert result["cleanup_state"] == "done"
        raw = bot.store.one("SELECT * FROM notes WHERE id=?", (note["id"],))
        assert raw["revision"] == (1 if action == "move_note" else 0)
        sent = len(bot.telegram.sent)
        await bot.ai_note_actions.execute(1, request, 1)
        assert len(bot.telegram.sent) == sent
        assert bot.store.one("SELECT count(*) AS n FROM ai_note_jobs")["n"] == 1
    asyncio.run(run())


def test_failed_telegram_delete_is_reported_and_cleanup_can_retry(bot):
    async def run():
        note = ready_note(bot)
        request = request_for(bot)
        read(bot, request, note)
        original = bot.telegram.call
        async def unavailable(method, **payload):
            if method == "deleteMessage":
                raise ProviderError("telegram_transport_unknown")
            return await original(method, **payload)
        bot.telegram.call = unavailable
        result = await act(bot, request, "delete_note", note)
        assert result["ok"] and result["cleanup_state"] == "pending" and "не удалось" in result["message"]
        _, changes = bot.ai_result_controls(1, request, bot.ai_state.request(1, request))
        assert result["message"] in changes
        bot.telegram.call = original
        await bot.ai_note_actions.cleanup(request, 1)
        assert bot.store.one("SELECT state FROM ai_note_jobs")["state"] == "done"
    asyncio.run(run())


def test_update_cannot_bypass_move_limit_and_folder_tool_is_complete_owner_scoped(bot):
    note = ready_note(bot)
    request = request_for(bot, note)
    folders = [bot.store.create_folder(1, f"Папка {i}") for i in range(12)]
    secret = bot.store.create_folder(2, "Секрет")
    listed = tool(bot, request, step("list_folders"))["folders"]
    assert len(listed) == 13 and secret["id"] not in {f["id"] for f in listed}
    read(bot, request, note)
    result = tool(bot, request, step("update_note", note_id=note["id"], revision=0, title="Новый план",
                                     content="Текст", folder_id=folders[0]["id"]))
    assert "move_note" in result["error"]
    assert bot.store.note(1, note["id"])["folder_id"] == note["folder_id"]


def test_new_folder_and_move_roll_back_together_if_receipt_cannot_be_saved(bot, monkeypatch):
    async def run():
        note = ready_note(bot)
        request = request_for(bot)
        read(bot, request, note)
        await act(bot, request, "move_note", note, title="Новая папка")
        def failure(*args):
            raise OSError("disk failure")
        monkeypatch.setattr(bot.ai_note_actions, "save_result", failure)
        with pytest.raises(OSError):
            await bot.ai_note_actions.choose(1, request, 1, "new")
        assert len(bot.store.folders(1)) == 1
        assert bot.store.note(1, note["id"])["folder_id"] == note["folder_id"]
        assert bot.store.all("SELECT * FROM ai_note_jobs") == []
    asyncio.run(run())


def test_miniapp_folder_choice_uses_same_permissions_and_one_time_move(tmp_path):
    from test_miniapp import client_for

    from voice_memory_bot.config import Limits
    from voice_memory_bot.store import Store

    async def run():
        store = Store(tmp_path / "web.sqlite3", Limits())
        store.ensure_user(1)
        try:
            async with client_for(tmp_path, store) as (app, mini, client):
                note = ready_note(app)
                request = request_for(app)
                read(app, request, note)
                await act(app, request, "move_note", note, title="Отпуск")
                app.ai_state.finish_reply(1, request, "Выбери папку")
                public = mini.public_note(store.note(1, request))
                assert public["ai"]["folder_choices"][0]["name"] == "Отпуск"
                url = f"/api/notes/{request}/ai-folder"
                payload = {"step": 1, "target": "new"}
                client.session.headers["Authorization"] = "Bearer " + mini.sign("session", 2)
                assert (await client.post(url, json=payload)).status == 400
                client.session.headers["Authorization"] = "Bearer " + mini.sign("session", 1)
                assert (await client.post(url, json=payload)).status == 200
                assert (await client.post(url, json=payload)).status == 200
                assert len(store.folders(1)) == 2
                assert store.note(1, note["id"])["revision"] == 1
                assert mini.public_note(store.note(1, request))["ai"]["folder_choices"] == []
        finally:
            store.close()
    asyncio.run(run())
