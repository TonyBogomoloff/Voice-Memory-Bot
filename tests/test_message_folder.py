from support.ai import bot as bot
from support.ai import ready_note, step, tool
from support.note_actions import request_for

from voice_memory_bot.assistant import validate_step


def test_selected_message_folder_tracks_move(bot):
    note = ready_note(bot)
    request = request_for(bot, note)
    command = validate_step(step("get_message_folder", note_id=note["id"]))
    assert tool(bot, request, command)["folder"]["id"] == note["folder_id"]
    folder = bot.store.create_folder(1, "Новая папка")
    bot.store.update_note(1, note["id"], folder_id=folder["id"])
    assert tool(bot, request, command) == {"note_id": note["id"], "folder": {
        "id": folder["id"], "name": "Новая папка", "mode": "notes"}}
    other = ready_note(bot, message_id=999)
    assert "error" in tool(bot, request, step("get_message_folder", note_id=other["id"]))


def test_folder_access_ownership_deleted_and_current_request(bot):
    request = request_for(bot)
    command = step("get_message_folder", note_id=request)
    bot.ai_state.set_settings(1, notes_access="read")
    assert "folder" in tool(bot, request, command)
    foreign = ready_note(bot, owner=2)
    assert "error" in tool(bot, request, step("get_message_folder", note_id=foreign["id"]))
    note = ready_note(bot)
    bot.store.db.execute("UPDATE notes SET deleted=1 WHERE id=?", (note["id"],))
    bot.store.db.commit()
    assert "error" in tool(bot, request, step("get_message_folder", note_id=note["id"]))
    bot.ai_state.set_settings(1, notes_access="none")
    assert "error" in tool(bot, request, command)
