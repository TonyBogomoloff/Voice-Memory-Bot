from support.ai import ready_note


def request_for(bot, note=None):
    request = ready_note(bot, "Перенеси или удали выбранную запись", message_id=901)
    conversation = bot.ai_state.start_note_conversation(1, note["id"]) if note else None
    bot.ai_state.enqueue(1, request["id"], "button", conversation=conversation)
    return request["id"]
