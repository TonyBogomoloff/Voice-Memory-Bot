from __future__ import annotations

import unicodedata

from .store import new_id


def folder_button_rows(buttons):
    """Estimate label width; Telegram chooses the final pixel widths."""
    rows, row = [], []
    widest = 0
    for item in buttons:
        width = sum(0 if unicodedata.combining(char) else
                    2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
                    for char in item["text"])
        candidate_width = max(widest, width)
        if row and (len(row) == 3 or (candidate_width + 4) * (len(row) + 1) > 36):
            rows.append(row)
            row, widest = [], 0
        row.append(item)
        widest = max(widest, width)
    if row:
        rows.append(row)
    return rows


class ReplyMoveMixin:

    def reply_note(self, owner, message):
        target = (message.get("reply_to_message") or {}).get("message_id")
        if not target:
            if "reply_to_message" in message or not self.store.folders(owner):
                return None
            folder = self.store.current_folder(owner)
            return self.store.one(
                "SELECT * FROM notes WHERE owner=? AND folder_id=? AND deleted=0 "
                "AND origin='telegram' AND message_id>0 ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (owner, folder))
        if message["reply_to_message"].get("chat", {}).get("id", owner) != owner:
            return None
        return self.store.one(
            "SELECT n.* FROM notes n WHERE n.owner=? AND n.deleted=0 AND "
            "(n.message_id=? OR n.transcript_message_id=? OR n.result_message_id=? OR EXISTS "
            "(SELECT 1 FROM note_messages m WHERE m.owner=n.owner AND m.note_id=n.id "
            "AND m.message_id=? AND m.deleted=0))",
            (owner, target, target, target, target))

    async def move_reply(self, owner, thread, message):
        note = self.reply_note(owner, message)
        if not note:
            return await self._show(owner, "В этой папке нет подходящей записи. Пришли сообщение или ответь на сохранённую запись.", thread)
        token = new_id()
        # Keep successful commands, but retain their exact IDs for cancellation.
        cleanup_command_id = message["message_id"]
        with self.store.transaction():
            self.store.db.execute("INSERT INTO reply_moves(token,owner,note_id,command_id) VALUES (?,?,?,?)",
                                  (token, owner, note["id"], cleanup_command_id))
        self.store.set_action(owner, None)
        await self.reply_move_menu(owner, thread, token)

    async def reply_move_menu(self, owner, thread, token, page=0):
        from .bot import button, keyboard
        session = self.store.one("SELECT * FROM reply_moves WHERE owner=? AND token=?", (owner, token))
        if not session:
            return
        source = self.store.note(owner, session["note_id"])["folder_id"]
        folders = [f for f in self.store.folders(owner) if f["id"] != source]
        page = max(0, min(page, (len(folders) - 1) // 8))
        rows = folder_button_rows([button(folder["name"], f"rmove:{token}:{folder['id']}")
                                   for folder in folders[page * 8:(page + 1) * 8]])
        nav = []
        if page:
            nav.append(button("←", f"rpage:{token}:{page - 1}"))
        if len(folders) > (page + 1) * 8:
            nav.append(button("→", f"rpage:{token}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.extend([[button("＋ Новая папка", f"rnew:{token}")], [button("Отмена", f"rcancel:{token}")]])
        result = await self.show_bottom(owner, "В какую папку ты хочешь переместить запись?", thread,
                                  keyboard(*rows), screen=f"replymove:{token}", keep=True, mirror=True, command_id=session["command_id"])
        with self.store.transaction():
            self.store.db.execute("UPDATE reply_moves SET prompt_id=? WHERE owner=? AND token=?",
                                  (result["message_id"], owner, token))

    async def finish_reply_move(self, owner, token, *, cancelled=False):
        session = self.store.one("SELECT * FROM reply_moves WHERE owner=? AND token=?", (owner, token))
        if not session:
            return
        await self.remove_menu_message(owner, session["prompt_id"])
        if cancelled:
            await self.remove_menu_message(owner, session["command_id"])
        if self.store.action(owner) == f"replymove:{token}":
            self.store.set_action(owner, None)
        with self.store.transaction():
            self.store.db.execute("DELETE FROM reply_moves WHERE owner=? AND token=?", (owner, token))

    async def reply_move_callback(self, owner, thread, message_id, data):
        from .bot import button, keyboard
        parts = data.split(":")
        if parts[0] not in {"rmove", "rpage", "rnew", "rcancel"}:
            return False
        if len(parts) < 2:
            return True
        action, token = parts[:2]
        session = self.store.one("SELECT * FROM reply_moves WHERE owner=? AND token=?", (owner, token))
        state = self.bottom_state(owner) or {}
        if not session or (session["prompt_id"] != message_id and not (
                message_id is None and state.get("screen") == f"replymove:{token}")):
            return True
        if action == "rcancel":
            if state.get("screen") == f"replymove:{token}":
                await self.cancel_record_menu(owner, thread, command_id=session["command_id"])
            await self.finish_reply_move(owner, token, cancelled=True)
        elif action == "rpage":
            await self.reply_move_menu(owner, thread, token, int(parts[2]))
        elif action == "rnew":
            self.store.note(owner, session["note_id"])
            self.store.set_action(owner, f"replymove:{token}")
            result = await self.show_bottom(owner, "Как назвать новую папку?", thread,
                keyboard([button("Отмена", f"rcancel:{token}")]), screen=f"replymove:{token}", keep=True, mirror=True,
                command_id=session["command_id"])
            with self.store.transaction():
                self.store.db.execute("UPDATE reply_moves SET prompt_id=? WHERE owner=? AND token=?",
                                      (result["message_id"], owner, token))
        elif action == "rmove":
            result = await self.move(owner, session["note_id"], parts[2], thread)
            await self.finish_reply_move(owner, token)
            await self.show_home(owner, thread, title=result)
        return True

    async def reply_move_new_folder(self, owner, thread, token, name):
        session = self.store.one("SELECT * FROM reply_moves WHERE owner=? AND token=?", (owner, token))
        if not session:
            self.store.set_action(owner, None)
            raise ValueError("Перенос уже отменён. Ответь на запись командой /move ещё раз.")
        note = self.store.note(owner, session["note_id"])
        if note["state"] in {"queued", "processing"} or self.note_locks[note["id"]].locked():
            raise ValueError("Дождись завершения обработки, затем перенеси запись")
        if not session["target_folder"]:
            folder = self.store.create_folder(owner, name)
            with self.store.transaction():
                self.store.db.execute("UPDATE reply_moves SET target_folder=? WHERE owner=? AND token=?",
                                      (folder["id"], owner, token))
            session["target_folder"] = folder["id"]
        result = await self.move(owner, session["note_id"], session["target_folder"], thread)
        await self.finish_reply_move(owner, token)
        await self.show_home(owner, thread, title=result)
