from __future__ import annotations

import json
import time

from .store import new_id

MENU_COMMANDS = {
    "📁 Папки": "/folders", "📝 Записи": "/notes",
    "＋": "/new", "＋ Новая папка": "/new", "🔎 Поиск": "/search",
    "🤖 ИИ": "/ai", "⚙️ Настройки": "/settings",
    "📂 Все папки": "/folders", "☰ Меню": "/menu",
    "⌂ Меню": "/menu", "✖️ Отмена": "/cancel",
    "⚙️ Options": "/options", "📁 Папка": "/options",
}
FOLDER_PREFIXES = ("📁 ", "✅ ", "📌 ", "✓ 📌 ")
OPTION_ACTIONS = {"✏️ Переименовать": "rename", "🗑 Удалить папку": "dropfolder",
                  "📦 Скачать исходники": "raw", "← К папке": "back"}


class FolderMenuMixin:

    def options_folder(self, owner):
        row = self.store.one("SELECT folder_id FROM folder_options WHERE owner=?", (owner,))
        if row and row['folder_id'] == self.store.current_folder(owner):
            return row['folder_id']
        return None

    def leave_folder_options(self, owner):
        with self.store.transaction():
            self.store.db.execute("DELETE FROM folder_options WHERE owner=?", (owner,))

    def enter_folder_options(self, owner, identity):
        folder = self.store.folder(owner, identity)
        if folder['removed']:
            raise ValueError('Папка удалена. Выбери другую')
        self.store.select(owner, identity)
        self.store.set_action(owner, None)
        with self.store.transaction():
            self.store.db.execute("INSERT INTO folder_options VALUES (?,?) ON CONFLICT(owner) "
                                  "DO UPDATE SET folder_id=excluded.folder_id", (owner, identity))
        return folder

    async def show_folder_options(self, owner, thread=None, identity=None):
        identity = identity or self.store.current_folder(owner)
        if not identity:
            return await self.ask_first_folder(owner, thread)
        return await self.show_notes_menu(owner, thread, identity)

    async def folder_option_button(self, owner, thread, text):
        identity = self.options_folder(owner)
        if not identity:
            return await self.refresh_folder_keyboard(owner, thread, force=True,
                notice='Открой меню выбранной папки заново.')
        action = OPTION_ACTIONS[text]
        if action == 'back':
            return await self.show_notes(owner, thread, identity)
        if action == 'raw':
            return await self.callback(owner, thread, None, f'zip:{identity}:raw')
        return await self.callback(owner, thread, None, f'{action}:{identity}')

    async def configure_menu(self):
        # Restore the command menu globally and for existing per-chat overrides.
        await self.telegram.call("setChatMenuButton", menu_button={"type": "commands"})
        for owner in sorted(self.config.allowed_user_ids):
            await self.telegram.call("setChatMenuButton", chat_id=owner, menu_button={"type": "commands"})

    def note_controls(self, note, thread):
        if not self.store.message_buttons_enabled(note["owner"]):
            return {"inline_keyboard": []}
        if note.get("entry_type") == "ai" or self.ai_state.request(note["owner"], note["id"]):
            return {"inline_keyboard": []}
        rows = [[
            {"text": "Переместить", "callback_data": f"pick:{note['id']}:0"},
            {"text": "Удалить", "callback_data": f"delete:{note['id']}", "style": "danger"},
            {"text": "ИИ", "callback_data": f"aiask:{note['id']}"}]]
        answer = self.ai_state.result_request(note["owner"], note["id"])
        if answer:
            rows[0].pop()
        return {"inline_keyboard": rows}

    def folder_keyboard_pages(self, owner):
        pinned = self.store.pinned_folders(owner)
        pinned_ids = {folder["id"] for folder in pinned}
        others = [folder for folder in self.store.folders(owner) if folder["id"] not in pinned_ids]
        # Keep pinned shortcuts on their own first page, including intentional empty slots.
        return ([pinned] if pinned else []) + [others[i:i + 4] for i in range(0, len(others), 4)] or [[]]

    def folder_input_placeholder(self, owner):
        active = self.store.current_folder(owner)
        name = self.store.folder(owner, active)["name"] if active else "Выбери папку"
        return ("📁 " + name)[:64]

    def folder_keyboard(self, owner):
        active = self.store.current_folder(owner)
        pages = self.folder_keyboard_pages(owner)
        page = min(max(self.folder_pages.get(owner, 0), 0), len(pages) - 1)
        self.folder_pages[owner] = page
        buttons = [{"text": ("✅ " if folder["id"] == active else "📁 ") + folder["name"],
                    **({"style": "primary"} if folder["id"] == active else {})}
                   for folder in pages[page]]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        if len(pages) > 1:
            while len(rows) < 2:
                rows.append([])
            if page > 0:
                rows[1].insert(0, {"text": "⬅️"})
            if page < len(pages) - 1:
                rows[1].append({"text": "➡️"})
        return {"keyboard": rows + [[{"text": "＋"}, {"text": "📁 Папка"}, {"text": "☰ Меню"}]],
                "resize_keyboard": True, "is_persistent": True,
                "input_field_placeholder": self.folder_input_placeholder(owner)}

    async def turn_folder_page(self, owner, thread, text):
        self.leave_folder_options(owner)
        pages = self.folder_keyboard_pages(owner)
        page = min(self.folder_pages.get(owner, 0), len(pages) - 1)
        self.folder_pages[owner] = min(max(page + (1 if text == "➡️" else -1), 0), len(pages) - 1)
        self.store.set_action(owner, None)
        self.folder_delete_confirmations.pop(owner, None)
        await self.refresh_folder_keyboard(owner, thread, force=True,
            title=f"Папки · {self.folder_pages[owner] + 1}/{len(pages)}")
        self.remember_screen(owner, thread, "Папки внизу", {"bottom_menu": True})

    @staticmethod
    def is_folder_button(text):
        return text.startswith(FOLDER_PREFIXES) and "\n" not in text

    def folder_update_owner(self, update):
        message = update.get('message') or {}
        owner = message.get('from', {}).get('id')
        if (owner not in self.config.allowed_user_ids or message.get('chat', {}).get('id') != owner
                or message.get('chat', {}).get('type') != 'private' or not isinstance(message.get('text'), str)):
            return None
        text, active = self.emoji_keyboards.resolve(owner, message['text'].strip())
        if active is False or not self.is_folder_button(text):
            return None
        if any(text == prefix + folder['name'] for folder in self.store.folders(owner) for prefix in FOLDER_PREFIXES):
            return owner
        return None

    def coalesce_folder_updates(self, update):
        selected = [update]
        owner = self.folder_update_owner(update)
        if owner is None:
            return selected
        # A note, command, callback, failed event or another owner is an ordering barrier.
        for row in self.store.all('SELECT id,payload,state FROM updates WHERE id>? ORDER BY id LIMIT 99',
                                  (update['update_id'],)):
            candidate = json.loads(row['payload'])
            if row['state'] != 'pending' or self.folder_update_owner(candidate) != owner:
                break
            selected.append(candidate)
        with self.store.transaction():
            self.store.db.executemany("UPDATE updates SET state='processing' WHERE id=?",
                                      [(item['update_id'],) for item in selected[1:]])
        return selected

    async def remove_coalesced_taps(self, selected):
        if len(selected) < 2:
            return
        owner = selected[-1]['message']['from']['id']
        keep = selected[-1]['message']['message_id']
        # A normal render already removes these; a no-op or deferred render may leave them.
        async with self.view_locks[owner]:
            mids = {item['message']['message_id'] for item in selected[:-1]} - {keep}
            remaining = [mid for mid in mids if self.store.one(
                'SELECT 1 FROM telegram_messages WHERE owner=? AND message_id=? AND view_blocked=0', (owner, mid))]
            if remaining:
                await self.delete_view_batch(owner, remaining)

    async def select_folder_button(self, owner, thread, text, message_id=None):
        # Reply keyboards send text. Use full names, never truncated labels or a reused slot number.
        # Stale or unpinned names refresh navigation instead of becoming notes.
        for folder in self.store.folders(owner):
            if text in {prefix + folder["name"] for prefix in FOLDER_PREFIXES}:
                if folder["id"] == self.store.current_folder(owner):
                    view = self.store.one('SELECT folder_id,page,screen,dirty FROM folder_views WHERE owner=?', (owner,))
                    last_page = max(0, (folder['count'] - 1) // self.store.folder_page_size(owner))
                    # Selection is saved before Telegram I/O. A failed switch must be retryable.
                    if (view and view['folder_id'] == folder['id'] and not view['dirty']
                            and view['page'] == last_page and not view['screen']):
                        await self.remove_menu_message(owner, message_id)
                        return
                self.folder_delete_confirmations.pop(owner, None)
                return await self.show_notes(owner, thread, folder["id"], command_message_id=message_id)
        await self.remove_menu_message(owner, message_id)
        self.store.set_action(owner, None)
        await self.refresh_folder_keyboard(owner, thread, force=True,
            notice="Эта кнопка устарела. Выбери папку внизу или открой меню.")

    async def refresh_folder_keyboard(self, owner, thread=None, *, force=False, notice=None, transient=False, title=None):
        if not self.store.folders(owner):
            return await self.ask_first_folder(owner, thread)
        folder = self.store.folder(owner, self.store.current_folder(owner))
        keys = self.folder_keyboard(owner)
        signature = (folder["id"], folder["name"], folder["mode"], keys)
        if (not force and self.folder_keyboards.get(owner) == signature
                and (self.bottom_state(owner) or {}).get("screen") == "root"):
            return
        mode = 'диалог с ИИ' if folder['mode'] == 'chat' else 'заметки'
        text = (title or f"📁 {folder['name']}") + f"\nРежим: {mode}\nСохраняю сообщения в эту папку."
        if notice:
            text = notice + "\n\n" + text
        response = await self.send_bottom(owner, thread,
            {"screen": "root", "back": "root", "text": text, "rows": [], "page": 0, "keep": bool(notice)},
            raw_keys=keys)
        self.folder_keyboards[owner] = signature
        return response

    async def show_home(self, owner, thread=None, *, title=None):
        self.leave_folder_options(owner)
        if not self.store.folders(owner):
            return await self.ask_first_folder(owner, thread)
        self.store.set_action(owner, None)
        self.folder_delete_confirmations.pop(owner, None)
        await self.refresh_folder_keyboard(owner, thread, force=True, title=title)
        self.remember_screen(owner, thread, "Папки внизу", {"bottom_menu": True})

    async def show_main_menu(self, owner, thread=None, edit_id=None, *, notice=None):
        self.leave_folder_options(owner)
        from .bot import button, keyboard
        self.store.set_action(owner, None)
        self.folder_delete_confirmations.pop(owner, None)
        identity = self.store.current_folder(owner)
        folder = self.store.folder(owner, identity) if identity else None
        enabled = self.store.message_buttons_enabled(owner)
        rows = [[button("Скрывать кнопки действий" if enabled else "Отправлять кнопки действий",
                        f"menumessagebuttons:{0 if enabled else 1}")],
                [button("ℹ️ Помощь", "help")]]
        await self.show_bottom(owner, notice or ("🎛️ Меню" + (f" · {folder['name']}" if folder else "")), thread, keyboard(*rows),
                               screen="home", back="root", edit_id=edit_id)

    async def folder_menu_callback(self, owner, thread, message_id, data):
        from .bot import button, keyboard
        action, _, identity = data.partition(":")
        if action == "home":
            await self.show_main_menu(owner, thread, message_id)
        elif action == "foldermenu":
            await self.show_notes_menu(owner, thread, identity, edit_id=message_id)
        elif action in {"pin", "unpin"}:
            self.store.set_folder_pin(owner, identity, action == "pin")
            await self.show_notes_menu(owner, thread, identity, edit_id=message_id)
        elif action == "dropfolder":
            folder = self.store.folder(owner, identity)
            count = self.store.one("SELECT count(*) AS n FROM notes WHERE owner=? AND folder_id=? AND deleted=0",
                                   (owner, identity))["n"]
            token = new_id()
            self.folder_delete_confirmations[owner] = (token, identity, folder["name"], time.monotonic() + 600)
            await self.show_bottom(owner, f"Удалить папку «{folder['name']}»?\n"
                            f"Все записи ({count}) и история ИИ останутся в архиве без папки.\n"
                            "Исходные файлы сохранятся; записи доступны через поиск и экспорт.", thread,
                            keyboard([button("Удалить", f"confirmdrop:{token}"),
                                      button("Отмена", f"canceldrop:{token}")]),
                            screen=f"dropfolder:{identity}", back=f"foldermenu:{identity}",
                            keep=True, edit_id=message_id, mirror=True)
        elif action in {"confirmdrop", "canceldrop"}:
            confirmation = self.folder_delete_confirmations.get(owner)
            if not confirmation or confirmation[0] != identity or confirmation[3] < time.monotonic():
                raise ValueError("Подтверждение устарело. Открой папку заново")
            _, folder_id, name, _ = confirmation
            folder = self.store.folder(owner, folder_id)
            if action == "canceldrop":
                self.folder_delete_confirmations.pop(owner, None)
                if self.options_folder(owner) == folder_id:
                    await self.show_folder_options(owner, thread, folder_id)
                else:
                    await self.show_notes(owner, thread, folder_id, edit_id=message_id)
                return True
            if folder["name"] != name:
                raise ValueError("Название папки изменилось. Открой её заново перед удалением")
            if self.deletion_busy(owner):
                raise ValueError("Дождись завершения обработки или экспорта, затем удали папку")
            selected = self.store.delete_folder(owner, folder_id)
            self.leave_folder_options(owner)
            self.folder_delete_confirmations.pop(owner, None)
            if selected:
                await self.show_notes(owner, thread, selected,
                                      notice=f"Папка «{name}» удалена. Записи доступны через поиск и экспорт.")
            else:
                await self.clear_folder_view(owner, thread, all_threads=True)
                with self.store.transaction():
                    self.store.db.execute('DELETE FROM folder_views WHERE owner=?', (owner,))
                await self.ask_first_folder(owner, thread)
        else:
            return False
        return True
