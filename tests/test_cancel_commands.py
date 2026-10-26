import asyncio

import pytest
from support.base import make_bot
from support.commands import bot as bot
from support.commands import receive


async def reply_command(bot, command, mid=400):
    await bot.handle({"message": {"message_id": mid, "from": {"id": 1},
        "chat": {"id": 1, "type": "private"}, "text": command,
        "reply_to_message": {"message_id": 100}}})


def track_messages(bot):
    sent = []
    original = bot.telegram.text

    async def record(*args, **kwargs):
        result = await original(*args, **kwargs)
        sent.append((result["message_id"], args[1]))
        return result

    bot.telegram.text = record
    return sent


def deleted(bot):
    return {p["message_id"] for method, p in bot.telegram.sent if method == "deleteMessage"}


@pytest.mark.parametrize("command", ["/move", "/delete", "/options", "/ai"])
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("inline", [False, True])
def test_cancel_removes_only_dialog_and_command_preserving_keyboard_and_record(bot, tmp_path, command, restart, inline):
    async def run():
        bot.telegram.message_sequence = 1000
        sent = track_messages(bot)
        await bot.show_home(1)
        original_menu = bot.store.one("SELECT * FROM bottom_menus WHERE owner=1")
        await receive(bot, "Исходная запись", 100)
        note = bot.store.notes(1)[0]
        folder = bot.store.folder(1, note["folder_id"])
        await reply_command(bot, command)
        assert original_menu["message_id"] not in deleted(bot)
        assert 400 not in deleted(bot)
        state = bot.bottom_state(1)
        if command == "/ai":
            conversation = bot.store.action(1).split(":")[1]
            prompt = bot.store.one("SELECT message_id FROM ai_instruction_prompts WHERE conversation_id=?", (conversation,))["message_id"]
            callback = f"aicancel:{conversation}"
        else:
            prompt, callback = state.get("inline_prompt_id"), state["actions"]["Отмена"]
        before_cancel = len(sent)
        current = bot
        if restart:
            current = make_bot(tmp_path, bot.store)
            current.telegram.message_sequence = bot.telegram.message_sequence
            sent = track_messages(current)
            before_cancel = 0
        if command == "/ai" or (inline and command != "/options"):
            await current.callback(1, None, prompt, callback)
        else:
            await receive(current, "Отмена", 500)
        removed = deleted(current)
        assert 400 in removed and 100 not in removed
        assert original_menu["message_id"] not in removed
        assert all(mid in removed for mid, _ in sent[before_cancel:])
        assert not any(text.startswith("📁") for _, text in sent[before_cancel:])
        assert current.store.note(1, note["id"]) == note
        assert current.store.folder(1, note["folder_id"]) == folder
        assert current.store.action(1) is None and current.ai_state.claim() is None
        assert current.store.one("SELECT * FROM bottom_menus WHERE owner=1") == original_menu
        assert not current.store.all("SELECT * FROM reply_moves")
        if prompt:
            assert prompt in removed
            # A duplicate tap cannot revive the cancelled flow or remove the record.
            await current.callback(1, None, prompt, callback)
            assert current.store.note(1, note["id"]) == note
            assert current.bottom_state(1)["screen"] == "root"
    asyncio.run(run())


@pytest.mark.parametrize("label", ["Перенести запись", "Удалить запись", "Изменить с ИИ"])
@pytest.mark.parametrize("inline", [False, True])
def test_options_child_cancel_removes_original_options_command(bot, label, inline):
    async def run():
        bot.telegram.message_sequence = 1000
        await bot.show_home(1)
        original_menu = bot.store.one("SELECT * FROM bottom_menus WHERE owner=1")
        await receive(bot, "Исходник", 100)
        note = bot.store.notes(1)[0]
        await reply_command(bot, "/options")
        await receive(bot, label, 450)
        state = bot.bottom_state(1)
        bot.telegram.sent.clear()
        sent = track_messages(bot)
        if label == "Изменить с ИИ" and inline:
            conversation = bot.store.action(1).split(":")[1]
            prompt = bot.store.one("SELECT message_id FROM ai_instruction_prompts WHERE conversation_id=?", (conversation,))
            await bot.callback(1, None, prompt["message_id"], f"aicancel:{conversation}")
        elif inline:
            await bot.callback(1, None, state["inline_prompt_id"], state["actions"]["Отмена"])
        else:
            await receive(bot, "Отмена", 500)
        assert 400 in deleted(bot)
        assert all(mid in deleted(bot) for mid, _ in sent)
        assert bot.store.one("SELECT * FROM bottom_menus WHERE owner=1") == original_menu
        assert bot.store.note(1, note["id"]) == note
        assert bot.store.action(1) is None and bot.ai_state.claim() is None
    asyncio.run(run())


def test_old_ai_cancel_does_not_remove_new_command_or_reset_new_flow(bot):
    async def run():
        bot.telegram.message_sequence = 1000
        await bot.show_home(1)
        await receive(bot, "Исходник", 100)
        await reply_command(bot, "/ai")
        old = bot.store.action(1).split(":")[1]
        prompt = bot.store.one("SELECT message_id FROM ai_instruction_prompts WHERE conversation_id=?", (old,))
        await reply_command(bot, "/ai", 500)
        active = bot.store.action(1)
        bot.telegram.sent.clear()
        bot.store.ensure_user(2)
        await bot.callback(2, None, prompt["message_id"], f"aicancel:{old}")
        assert 400 not in deleted(bot)
        await bot.callback(1, None, prompt["message_id"], f"aicancel:{old}")
        assert 400 in deleted(bot) and 500 not in deleted(bot)
        assert bot.store.action(1) == active
        assert bot.store.notes(1)[0]["message_id"] == 100
    asyncio.run(run())


def test_consumed_ai_command_is_kept_when_old_cancel_is_tapped(bot):
    async def run():
        bot.telegram.message_sequence = 1000
        await bot.show_home(1)
        await receive(bot, "Исходник", 100)
        await reply_command(bot, "/ai")
        conversation = bot.store.action(1).split(":")[1]
        prompt = bot.store.one("SELECT message_id FROM ai_instruction_prompts WHERE conversation_id=?", (conversation,))
        await receive(bot, "Сделай короче", 500)
        assert bot.store.action(1) is None
        bot.telegram.sent.clear()
        await bot.callback(1, None, prompt["message_id"], f"aicancel:{conversation}")
        assert 400 not in deleted(bot)
        assert len(bot.store.all("SELECT * FROM ai_requests")) == 1
    asyncio.run(run())
