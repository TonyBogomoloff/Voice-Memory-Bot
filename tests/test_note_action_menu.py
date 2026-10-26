import asyncio

import pytest
from support.base import make_bot, receive_voice


@pytest.mark.parametrize("screen", ["root", "settings"])
@pytest.mark.parametrize("action", ["delete", "cancel", "move", "legacy_move"])
def test_note_actions_preserve_bottom_menu(tmp_path, store, screen, action):
    async def run():
        bot = make_bot(tmp_path, store)
        bot.telegram.message_sequence = 1000
        target = store.create_folder(1, "Другая папка")
        await receive_voice(bot, 100, 200)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        if screen == "root":
            await bot.show_home(1, 200)
        else:
            await bot.settings_menu(1, 200)
        active = store.current_folder(1)
        bot.telegram.sent.clear()
        if action in {"delete", "cancel"}:
            await bot.callback(1, 200, note["transcript_message_id"], f"delete:{note['id']}")
            prompt_id = bot.telegram.message_sequence
            assert bot.telegram.sent[-1][1]["keyboard"]["inline_keyboard"]
            state = bot.bottom_state(1)
            assert state["screen"] == f"notedelete:{note['id']}"
            await bot.callback(1, 200, prompt_id,
                               state["actions"]["Удалить" if action == "delete" else "Отмена"])
        else:
            picker = "pick" if action == "move" else "choose"
            await bot.callback(1, 200, note["transcript_message_id"], f"{picker}:{note['id']}:0")
            assert bot.bottom_state(1)["screen"].startswith("replymove:")
            await bot.bottom_input(1, 200, "Другая папка", 9000)
            assert store.note(1, note["id"])["folder_id"] == target["id"]
            assert store.current_folder(1) == active
            assert bot.bottom_state(1)["screen"] == "root"
            return
        expected_screen = "settings:all" if action == "cancel" and screen == "settings" else "root"
        assert bot.bottom_state(1)["screen"] == expected_screen
        assert store.current_folder(1) == active
    asyncio.run(run())


def test_legacy_bottom_move_result_preserves_keyboard(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        bot.telegram.message_sequence = 1000
        target = store.create_folder(1, "Другая папка")
        await receive_voice(bot, 100, 200)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        await bot.show_bottom(1, "Куда переместить?", 200, {"inline_keyboard": [[
            {"text": "Другая папка", "callback_data": f"move:{note['id']}:{target['id']}"}]]})
        before = bot.bottom_state(1)
        bot.telegram.sent.clear()
        assert await bot.bottom_input(1, 200, "Другая папка", 5000)
        assert bot.bottom_state(1) == before
        assert store.note(1, note["id"])["folder_id"] == target["id"]
        assert not any("keyboard" in (p.get("keyboard") or {}) for _, p in bot.telegram.sent)
    asyncio.run(run())
