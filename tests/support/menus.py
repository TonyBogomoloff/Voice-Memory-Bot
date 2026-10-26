

def reply_keyboard(bot):
    return next(payload["keyboard"] for method, payload in reversed(bot.telegram.sent)
                if method in {"sendMessage", "sendVideo"} and "keyboard" in (payload.get("keyboard") or {}))


def labels(bot):
    return [button["text"] for row in reply_keyboard(bot)["keyboard"] for button in row]
