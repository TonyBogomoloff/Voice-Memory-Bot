from __future__ import annotations

import logging
import time

from .delivery import deletion_error
from .providers import ProviderError, silent_delivery
from .states import MenuState, Target
from .store import new_id

log = logging.getLogger(__name__)
BACK = "⬅️ Назад"
ROOT = "🏠 Главная"
PREVIOUS = "◀️ Страница"
NEXT = "Страница ▶️"
CONFIRMATIONS = {"🗑 Да, удалить папку", "🗑 Да, удалить запись", "🗑 Да, удалить всё"}
PAGE_SIZE = 6


class BottomMenuMixin:
    """Reply keyboard navigation. Labels resolve to the exact rendered action, per owner."""

    def init_bottom_menu(self):
        self.bottom_epoch = new_id()
        self.command_cleanups = {}

    def menu_state(self, owner):
        row = self.store.one("SELECT state FROM bottom_menus WHERE owner=?", (owner,))
        return MenuState.from_json(row["state"]) if row else None

    def bottom_state(self, owner):
        # Compatibility adapter for existing handlers and saved view payloads.
        state = self.menu_state(owner)
        return state.to_mapping() if state else None

    def is_bottom_control(self, owner, text):
        return text in {BACK, ROOT, PREVIOUS, NEXT} | CONFIRMATIONS or bool(self.store.one(
            "SELECT 1 FROM bottom_menu_labels WHERE owner=? AND label=?", (owner, text)))

    async def remove_menu_message(self, owner, message_id):
        if not message_id:
            return
        pending = self.command_cleanups.get(owner)
        if pending and pending["message_id"] == message_id:
            pending["remove"] = True
            return
        try:
            await self.view_send(self.telegram.call, "deleteMessage", chat_id=owner, message_id=message_id)
        except ProviderError as exc:
            if not deletion_error(exc).complete:
                # Retain the receipt so the regular view cleanup can retry later.
                log.info("menu cleanup deferred owner=%s message_id=%s", owner, message_id)
                return
        with self.store.transaction():
            self.store.db.execute("DELETE FROM telegram_messages WHERE owner=? AND message_id=?", (owner, message_id))

    async def show_bottom(self, owner, text, thread=None, keys=None, *, screen="home", back="root",
                          keep=False, page=0, edit_id=None):
        if screen.split(":")[0] not in {"foldermenu", "rename", "dropfolder"}:
            self.leave_folder_options(owner)
        rows = (keys or {}).get("inline_keyboard", [])
        # Navigation is always in the same last row, independent of the screen.
        rows = [[b for b in row if b.get("callback_data") != "home"
                 and not b["text"].startswith("← ") and (screen.startswith("replymove:")
                 or b["text"] not in {"Отмена", "✖️ Отмена"})]
                for row in rows]
        rows = [row for row in rows if row]
        state = MenuState(Target.parse(screen), Target.parse(back), text, rows, page, keep)
        if any(b.get("callback_data", "").startswith("confirm") for row in rows for b in row):
            state.epoch, state.expires = self.bottom_epoch, time.time() + 600
        self.remember_screen(owner, thread, text, {"bottom_menu": True})
        return await self.send_bottom(owner, thread, state, edit_id=edit_id)

    async def send_bottom(self, owner, thread, state, *, edit_id=None, raw_keys=None):
        if not isinstance(state, MenuState):
            state = MenuState.from_mapping(state)
        rows = state.rows
        page_size = max(1, len(rows)) if state.screen.kind == "replymove" else PAGE_SIZE
        pages = max(1, (len(rows) + page_size - 1) // page_size)
        page = state.page = min(max(0, state.page), pages - 1)
        actions, keyboard, used = {}, [], set()
        reserved = {BACK, ROOT, PREVIOUS, NEXT}
        for row_index, row in enumerate(rows):
            rendered = []
            for item in row:
                label = item["text"]
                if label in used or label in reserved:
                    number = 2
                    while f"{label} · {number}" in used:
                        number += 1
                    label = f"{label} · {number}"
                used.add(label)
                if not page * page_size <= row_index < (page + 1) * page_size:
                    continue
                actions[label] = item["callback_data"]
                rendered.append({"text": label})
                if item.get("style"):
                    rendered[-1]["style"] = item["style"]
            if rendered:
                keyboard.append(rendered)
        if pages > 1:
            keyboard.append(([{"text": PREVIOUS}] if page else []) +
                            ([{"text": NEXT}] if page + 1 < pages else []))
        if state.screen.kind == "ai_note":
            keyboard = [[{"text": "Отмена"}]]
            actions = {"Отмена": "aicancel:" + state.screen.argument}
        elif state.screen.wire == "settings:all":
            keyboard[1].append({"text": BACK})
            keyboard.append([{"text": ROOT}])
        elif state.screen.kind in {"create", "recordoptions"}:
            cancel = {"text": "Отмена"}
            if keyboard and len(keyboard[-1]) < 2:
                keyboard[-1].append(cancel)
            else:
                keyboard.append([cancel])
            actions["Отмена"] = "cancel_record_options"
        elif state.screen.kind == "foldermenu":
            keyboard[-1].insert(0, {"text": BACK})
        elif state.screen.kind not in {"root", "replymove"}:
            keyboard.append([{"text": BACK}, {"text": ROOT}])
        keys = raw_keys or {"keyboard": keyboard, "resize_keyboard": True, "is_persistent": True,
                            "input_field_placeholder": self.folder_input_placeholder(owner)}
        state.actions = actions
        previous = self.store.one("SELECT message_id FROM bottom_menus WHERE owner=?", (owner,))
        # Persist before send: a lost Telegram response must not turn the displayed controls into notes.
        with self.store.transaction():
            self.store.db.execute("INSERT INTO bottom_menus(owner,state) VALUES (?,?) ON CONFLICT(owner) "
                                  "DO UPDATE SET state=excluded.state", (owner, state.to_json()))
            self.store.db.executemany("INSERT OR IGNORE INTO bottom_menu_labels(owner,label) VALUES (?,?)",
                                      [(owner, label) for label in actions])
        if edit_id:
            try:
                await self.edit_note_buttons(owner, edit_id, {"inline_keyboard": []})
            except ProviderError as exc:
                if str(exc) != "telegram_400":
                    raise
        token = silent_delivery.set(True)
        try:
            text = state.text if state.keep else state.text.split("\n", 1)[0]
            response = await self.view_send(self.telegram.text, owner, text, thread, keys)
        finally:
            silent_delivery.reset(token)
        with self.store.transaction():
            self.store.db.execute("UPDATE bottom_menus SET message_id=? WHERE owner=?",
                                  (response["message_id"], owner))
        # Telegram Web drops the keyboard (and can show START) when its carrier is
        # deleted. Keep one short heading; retire the previous carrier only after
        # the new keyboard has been delivered and its receipt saved.
        if previous and previous["message_id"] != response["message_id"]:
            await self.remove_menu_message(owner, previous["message_id"])
        return response

    async def bottom_input(self, owner, thread, text, message_id):
        from .folder_menu import MENU_COMMANDS
        state = self.menu_state(owner)
        if not self.is_bottom_control(owner, text):
            return False
        action = state.actions.get(text) if state else None
        if action is None and (text in MENU_COMMANDS or self.is_folder_button(text)):
            return False
        await self.remove_menu_message(owner, message_id)
        if text in {PREVIOUS, NEXT} and state:
            state.page += 1 if text == NEXT else -1
            await self.send_bottom(owner, thread, state)
            return True
        if text in {BACK, ROOT}:
            self.store.set_action(owner, None)
            self.delete_confirmations.pop(owner, None)
            self.folder_delete_confirmations.pop(owner, None)
            await self.open_bottom_screen(owner, thread, state.back.wire if state and text == BACK else "root")
            return True
        if action is None:
            # Old keyboards on another device and delayed taps never become archive input.
            await self.show_bottom(owner, "Эта кнопка устарела. Выбери действие внизу.", thread,
                                   screen="stale", back=state.screen.wire if state else "root", keep=True)
            self.store.set_action(owner, None)
            return True
        if action == "cancel_record_options":
            self.store.set_action(owner, None)
            if state.screen.wire == "create" and not self.store.folders(owner):
                await self.show_main_menu(owner, thread)
            else:
                await self.show_home(owner, thread)
            return True
        if state.screen.wire == "root":
            self.store.set_action(owner, None)
        if action.startswith("confirm"):
            # Consume the confirmation before yielding. A duplicate tap cannot repeat it.
            state.consume_confirmation(text, self.bottom_epoch, time.time())
            with self.store.transaction():
                self.store.db.execute("UPDATE bottom_menus SET state=? WHERE owner=?",
                                      (state.to_json(), owner))
        result = await self.callback(owner, thread, None, action)
        if isinstance(result, str) and result:
            if action.startswith(("place:", "move:")):
                await self._show(owner, result, thread)
            else:
                await self.show_bottom(owner, result, thread, screen="notice", back="root", keep=True)
        return True

    async def open_bottom_screen(self, owner, thread, screen):
        if screen == "root":
            return await self.show_home(owner, thread)
        if screen in {"home", "stale"}:
            return await self.show_main_menu(owner, thread)
        # Back can only reopen read-only menus, never replay a mutation or confirmation.
        if Target.parse(screen).can_reopen:
            return await self.callback(owner, thread, None, screen)
        return await self.show_main_menu(owner, thread)
