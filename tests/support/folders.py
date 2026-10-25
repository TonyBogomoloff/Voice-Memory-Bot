

def deleted_ids(bot):
    return {mid for method, payload in bot.telegram.sent if method in {'deleteMessage', 'deleteMessages'}
            for mid in payload.get('message_ids', [payload.get('message_id')])}


def saved(bot, folder, text, mid):
    note, _ = bot.store.add_note(1, folder, {'message_id': mid}, 'text', transcript=text)
    bot.store.update_note(1, note['id'], state='ready', delivery='sent', transcript_delivery='sent', title=text)
    bot.cleanup.remember(1, mid)
    return note
