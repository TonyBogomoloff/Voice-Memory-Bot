from __future__ import annotations

import time

from .store import new_id

MENU_COMMANDS = {
    "📁 Папки": "/folders", "📝 Записи": "/notes",
    "＋ Новая папка": "/new", "🔎 Поиск": "/search",
    "🤖 ИИ": "/ai", "⚙️ Настройки": "/settings",
    "⌂ Меню": "/start", "✖️ Отмена": "/cancel",
}
MAIN_MENU = {
    "keyboard": [[{"text": label} for label in row] for row in (
        ("📁 Папки", "📝 Записи"), ("＋ Новая папка", "🔎 Поиск"), ("🤖 ИИ", "⚙️ Настройки"))],
    "resize_keyboard": True, "is_persistent": True,
    "input_field_placeholder": "Голосовое, текст или фото в выбранную папку",
}


class FolderMenuMixin:
    async def configure_menu(self):
        # Clear both the default Mini App button and existing per-chat overrides.
        await self.telegram.call("setChatMenuButton", menu_button={"type": "commands"})
        for owner in sorted(self.config.allowed_user_ids):
            await self.telegram.call("setChatMenuButton", chat_id=owner, menu_button={"type": "commands"})

    def note_controls(self, note, thread):
        return {"inline_keyboard": [[{"text": "Опции", "callback_data": f"options:{note['id']}"}]]}

    async def show_home(self, owner, thread=None):
        self.store.set_action(owner, None)
        self.folder_delete_confirmations.pop(owner, None)
        folder = self.store.folder(owner, self.store.current_folder(owner))
        text = (f"🎙 Сейчас сохраняю в «{folder['name']}».\n"
                "Пришли голосовое, текст или фото. Для другой папки нажми «📁 Папки» внизу.")
        if self.config.web_url:
            text += "\nMini App: /app"
        await self.show(owner, text, thread, MAIN_MENU)

    async def folder_menu_callback(self, owner, thread, message_id, data):
        from .bot import button, keyboard
        action, _, identity = data.partition(":")
        if action == "home":
            await self.show_home(owner, thread)
        elif action == "dropfolder":
            folder = self.store.folder(owner, identity)
            if folder["is_inbox"]:
                raise ValueError("Папку «Входящие» удалить нельзя")
            count = self.store.one("SELECT count(*) AS n FROM notes WHERE owner=? AND folder_id=? AND deleted=0",
                                   (owner, identity))["n"]
            token = new_id()
            self.folder_delete_confirmations[owner] = (token, identity, folder["name"], time.monotonic() + 600)
            await self.show(owner, f"Удалить папку «{folder['name']}»?\n"
                            f"Все записи ({count}) и история ИИ сохранятся во «Входящих».\n"
                            "Исходные файлы и сообщения Telegram останутся.", thread,
                            keyboard([button("Удалить папку", f"confirmdrop:{token}")],
                                     [button("Отмена", f"canceldrop:{token}")]), message_id)
        elif action in {"confirmdrop", "canceldrop"}:
            confirmation = self.folder_delete_confirmations.get(owner)
            if not confirmation or confirmation[0] != identity or confirmation[3] < time.monotonic():
                raise ValueError("Подтверждение устарело. Открой папку заново")
            _, folder_id, name, _ = confirmation
            folder = self.store.folder(owner, folder_id)
            if action == "canceldrop":
                self.folder_delete_confirmations.pop(owner, None)
                await self.show_notes(owner, thread, folder_id, edit_id=message_id)
                return True
            if folder["name"] != name:
                raise ValueError("Название папки изменилось. Открой её заново перед удалением")
            if self.deletion_busy(owner):
                raise ValueError("Дождись завершения обработки или экспорта, затем удали папку")
            self.store.delete_folder(owner, folder_id)
            self.folder_delete_confirmations.pop(owner, None)
            await self.show(owner, f"Папка «{name}» удалена. Записи сохранены во «Входящих».", thread,
                            keyboard([button("📁 Папки", "folders"), button("⌂ Меню", "home")]), message_id)
        else:
            return False
        return True
