from __future__ import annotations

import time

from .store import new_id

MENU_COMMANDS = {
    "📁 Папки": "/folders", "📝 Записи": "/notes",
    "＋ Новая папка": "/new", "🔎 Поиск": "/search",
    "🤖 ИИ": "/ai", "⚙️ Настройки": "/settings",
    "📂 Все папки": "/folders", "☰ Меню": "/menu",
    "⌂ Меню": "/menu", "✖️ Отмена": "/cancel",
}
FOLDER_PREFIXES = ("📌 ", "✓ 📌 ")


class FolderMenuMixin:
    async def configure_menu(self):
        # Clear both the default Mini App button and existing per-chat overrides.
        await self.telegram.call("setChatMenuButton", menu_button={"type": "commands"})
        for owner in sorted(self.config.allowed_user_ids):
            await self.telegram.call("setChatMenuButton", chat_id=owner, menu_button={"type": "commands"})

    def note_controls(self, note, thread):
        return {"inline_keyboard": [[{"text": "Опции", "callback_data": f"options:{note['id']}"}]]}

    def folder_keyboard_pages(self, owner):
        pinned = self.store.pinned_folders(owner)
        pinned_ids = {folder["id"] for folder in pinned}
        others = [folder for folder in self.store.folders(owner) if folder["id"] not in pinned_ids]
        # Keep pinned shortcuts on their own first page, including intentional empty slots.
        return ([pinned] if pinned else []) + [others[i:i + 4] for i in range(0, len(others), 4)] or [[]]

    def folder_keyboard(self, owner):
        active = self.store.current_folder(owner)
        pages = self.folder_keyboard_pages(owner)
        page = min(max(self.folder_pages.get(owner, 0), 0), len(pages) - 1)
        self.folder_pages[owner] = page
        buttons = [{"text": ("✓ " if folder["id"] == active else "") + f"📌 {folder['name']}"}
                   for folder in pages[page]]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        if len(pages) > 1:
            while len(rows) < 2:
                rows.append([])
            if page > 0:
                rows[1].insert(0, {"text": "⬅️"})
            if page < len(pages) - 1:
                rows[1].append({"text": "➡️"})
        return {"keyboard": rows + [[{"text": "📂 Все папки"}, {"text": "☰ Меню"}]],
                "resize_keyboard": True, "is_persistent": True,
                "input_field_placeholder": "Голосовое, кружок, текст или фото"}

    async def turn_folder_page(self, owner, thread, text):
        pages = self.folder_keyboard_pages(owner)
        page = min(self.folder_pages.get(owner, 0), len(pages) - 1)
        self.folder_pages[owner] = min(max(page + (1 if text == "➡️" else -1), 0), len(pages) - 1)
        self.store.set_action(owner, None)
        self.folder_delete_confirmations.pop(owner, None)
        await self.refresh_folder_keyboard(owner, thread, force=True)

    @staticmethod
    def is_folder_button(text):
        return text.startswith(FOLDER_PREFIXES) and "\n" not in text

    async def select_folder_button(self, owner, thread, text):
        # Reply keyboards send text. Use full names, never truncated labels or a reused slot number.
        # Stale or unpinned names refresh navigation instead of becoming notes.
        for folder in self.store.folders(owner):
            if text in {prefix + folder["name"] for prefix in FOLDER_PREFIXES}:
                self.folder_delete_confirmations.pop(owner, None)
                return await self.show_notes(owner, thread, folder["id"])
        self.store.set_action(owner, None)
        await self.refresh_folder_keyboard(owner, thread, force=True,
            notice="Эта кнопка устарела. Выбери папку внизу или открой «Все папки».")

    async def refresh_folder_keyboard(self, owner, thread=None, *, force=False, notice=None):
        if not self.store.folders(owner):
            return await self.ask_first_folder(owner, thread)
        folder = self.store.folder(owner, self.store.current_folder(owner))
        keys = self.folder_keyboard(owner)
        signature = (folder["id"], folder["name"], folder["mode"], keys)
        if not force and self.folder_keyboards.get(owner) == signature:
            return
        mode = 'диалог с ИИ' if folder['mode'] == 'chat' else 'заметки'
        text = f"📁 {folder['name']}\nРежим: {mode}\nСохраняю сообщения в эту папку."
        if notice:
            text = notice + "\n\n" + text
        response = await self.view_send(self.telegram.text, owner, text, thread, keys)
        self.folder_keyboards[owner] = signature
        return response

    async def show_home(self, owner, thread=None):
        if not self.store.folders(owner):
            return await self.ask_first_folder(owner, thread)
        self.store.set_action(owner, None)
        self.folder_delete_confirmations.pop(owner, None)
        hint = "Переключай папки внизу. Стрелки листают страницы, действия — в «Меню»."
        if self.config.web_url:
            hint += "\nMini App: /app"
        await self.refresh_folder_keyboard(owner, thread, force=True, notice=hint)
        self.remember_screen(owner, thread, hint, self.folder_keyboard(owner))

    async def show_main_menu(self, owner, thread=None, edit_id=None):
        from .bot import HOME, button, keyboard
        if not self.store.folders(owner):
            return await self.ask_first_folder(owner, thread)
        self.store.set_action(owner, None)
        self.folder_delete_confirmations.pop(owner, None)
        await self.refresh_folder_keyboard(owner, thread)
        folder = self.store.folder(owner, self.store.current_folder(owner))
        await self.show(owner, f"☰ Меню\nВыбрана папка «{folder['name']}».", thread,
                        keyboard([button("Настройки папки", f"foldermenu:{folder['id']}")],
                                 *HOME["inline_keyboard"]), edit_id)

    async def folder_menu_callback(self, owner, thread, message_id, data):
        from .bot import button, keyboard
        action, _, identity = data.partition(":")
        if action == "home":
            await self.show_main_menu(owner, thread, message_id)
        elif action == "foldermenu":
            await self.show_notes_menu(owner, thread, identity, edit_id=message_id)
        elif action in {"pin", "unpin"}:
            self.store.set_folder_pin(owner, identity, action == "pin")
            await self.show_notes(owner, thread, identity, edit_id=message_id)
        elif action == "dropfolder":
            folder = self.store.folder(owner, identity)
            count = self.store.one("SELECT count(*) AS n FROM notes WHERE owner=? AND folder_id=? AND deleted=0",
                                   (owner, identity))["n"]
            token = new_id()
            self.folder_delete_confirmations[owner] = (token, identity, folder["name"], time.monotonic() + 600)
            await self.show(owner, f"Удалить папку «{folder['name']}»?\n"
                            f"Все записи ({count}) и история ИИ останутся в архиве без папки.\n"
                            "Исходные файлы сохранятся; записи доступны через поиск и экспорт.", thread,
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
            selected = self.store.delete_folder(owner, folder_id)
            self.folder_delete_confirmations.pop(owner, None)
            if selected:
                await self.show_notes(owner, thread, selected,
                                      notice=f"Папка «{name}» удалена. Записи доступны через поиск и экспорт.")
            else:
                await self.clear_folder_view(owner, thread, all_threads=True)
                with self.store.db:
                    self.store.db.execute('DELETE FROM folder_views WHERE owner=?', (owner,))
                await self.ask_first_folder(owner, thread)
        else:
            return False
        return True
