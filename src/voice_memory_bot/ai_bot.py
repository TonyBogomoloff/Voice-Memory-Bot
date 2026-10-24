from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

from .ai_note_actions import NoteActions
from .ai_store import AIStore
from .assistant import AssistantEngine, CodexAssistant, edge_candidate
from .providers import ProviderError

log = logging.getLogger(__name__)


def key(label, data):
    return {"text": label, "callback_data": data}


def keys(*rows):
    return {"inline_keyboard": list(rows)}


class AIBotMixin:
    def init_ai(self, assistant=None):
        self.ai_state = AIStore(self.store, is_busy=lambda identity: self.note_locks[identity].locked())
        self.ai_state.archive_replies()
        self.ai_models = tuple(dict.fromkeys((self.config.llm.model, *self.config.assistant_models)))
        self.assistant = assistant or CodexAssistant(self.config.llm)
        self.ai_note_actions = NoteActions(self)
        self.ai_engine = AssistantEngine(self.ai_state, self.assistant, self.config.llm.model, self.ai_models,
                                         self.archive, self.ai_note_actions)
        self.ai_delivery_locks = defaultdict(asyncio.Lock)

    def reply_input(self, owner, message):
        reply = message.get("reply_to_message") or {}
        if not reply.get("message_id") or "forum_topic_created" in reply:
            return None
        if reply.get("chat", {}).get("id", owner) != owner:
            return None
        return reply

    def reply_context(self, owner, reply):
        text = reply.get("text") or reply.get("caption")
        if not text:
            saved = self.store.one(
                "SELECT n.transcript,n.content FROM notes n JOIN note_messages m ON m.note_id=n.id "
                "AND m.owner=n.owner WHERE m.owner=? AND m.message_id=? AND n.deleted=0 AND m.deleted=0",
                (owner, reply["message_id"]))
            if saved:
                text = saved["content"] if saved["content"] is not None else saved["transcript"]
        return (text or "Текст исходного сообщения недоступен.")[:40000]

    def check_ai_capacity(self, owner, folder_id, message):
        folder = self.store.folder(owner, folder_id)
        reply = self.reply_input(owner, message)
        action = self.store.action(owner) or ""
        if action.startswith("ai_note:"):
            context = self.ai_state.note_context(owner, action.split(":", 1)[1])
            if context:
                self.store.note(owner, context["note_id"])
        if folder["mode"] == "chat" or reply or (self.store.action(owner) or "").startswith("ai_note:"):
            pending = self.store.one("SELECT count(*) AS n FROM ai_requests WHERE owner=? "
                                     "AND state IN ('queued','running')", (owner,))["n"]
            if pending >= self.store.limits.max_pending_per_user:
                raise ValueError("Очередь ИИ заполнена; дождись предыдущих ответов")

    def capture_ai_input(self, note, message, *, scoped=False):
        owner = note["owner"]
        source = json.loads(note.get("source_json") or "{}")
        if "reply_to_message" in source:
            message = {**message, "reply_to_message": source["reply_to_message"]}
        reply = self.reply_input(owner, message)
        conversation = self.ai_state.reply_conversation(owner, reply["message_id"]) if reply else None
        action = self.store.action(owner) or ""
        if scoped and not reply and action.startswith("ai_note:"):
            conversation = action.split(":", 1)[1]
            context = self.ai_state.note_context(owner, conversation)
            if context:
                self.store.note(owner, context["note_id"])
        if reply or conversation or note["input_mode"] == "chat":
            trigger = "reply" if reply or conversation else "folder"
            if not reply and not conversation and not self.ai_state.request(owner, note["id"]):
                conversation = self.ai_state.active(owner, note["source_folder"])
            self.ai_state.enqueue(owner, note["id"], trigger,
                                  message.get("message_thread_id"), message["message_id"], conversation,
                                  reply_context=self.reply_context(owner, reply) if reply else None)
            if scoped and action.startswith("ai_note:"):
                self.store.set_action(owner, None)

    async def route_ai(self, note, detect=True):
        owner, identity = note["owner"], note["id"]
        request = self.ai_state.request(owner, identity)
        if request is None and detect:
            settings = self.ai_state.settings(owner)
            if settings["wake_enabled"] and edge_candidate(note["transcript"], settings["names"]):
                try:
                    addressed = await self.assistant.addressed(note["transcript"], settings["names"],
                                                               self.ai_engine.model(owner))
                    # The toggle may have changed while the provider was responding.
                    if addressed and self.ai_state.settings(owner)["wake_enabled"]:
                        request = self.ai_state.enqueue(owner, identity, "name", note["source_thread"], note["message_id"])
                except (ProviderError, ValueError) as exc:
                    log.warning("AI address detection unavailable type=%s", type(exc).__name__)
        request = request or self.ai_state.request(owner, identity)
        if request is None:
            return False
        current = self.store.note(owner, identity)
        if (request["trigger"] == "name" and current["transcript_message_id"]
                and current["transcript_message_id"] != current["message_id"]):
            await self.edit_note_buttons(owner, current["transcript_message_id"], keys())
        self.store.update_note(owner, identity, state="ready", routing="ai", requested_folder=None,
                               title=" ".join((note["transcript"] or "").split())[:80] or "Диалог с ИИ",
                               delivery=current["transcript_delivery"],
                               result_message_id=current["transcript_message_id"], result_thread=note["source_thread"])
        self.archive.persist_text(self.store.note(owner, identity), self.store.folder(owner, current["folder_id"]))
        await self.show_ai_progress(request)
        return True

    async def ai_settings(self, owner, thread, edit_id=None):
        settings = self.ai_state.settings(owner)
        access_labels = {"none": "выключен", "read": "чтение", "write": "чтение и запись"}
        selected = settings["model"] or self.config.llm.model
        text = f"🤖 Настройки ИИ\nМодель: {selected}\nДоступ к заметкам: {access_labels[settings['notes_access']]}"
        rows = [[key("Обращение по имени: " + ("вкл" if settings["wake_enabled"] else "выкл"),
                     f"aiwake:{0 if settings['wake_enabled'] else 1}")]]
        if settings["wake_enabled"]:
            text += "\nИмена: " + ", ".join(settings["names"])
            rows.append([key("Изменить имена", "ainames")])
        rows.extend([[key("Доступ: " + access_labels[settings['notes_access']], "aiaccessmenu")],
                     [key("Модель: " + selected, "aimodels")]])
        text += "\n\nВ ИИ-папке все сообщения идут в диалог. Ответ на любое сообщение запускает ИИ с текстом этого сообщения как контекстом."
        text += "\nПри доступе к заметкам модель получает найденные записи по мере запроса."
        await self.show_bottom(owner, text, thread, keys(*rows), screen="aisettings",
                               back="settings:all", edit_id=edit_id)

    async def open_ai(self, owner, thread):
        folders = self.store.folders(owner)
        folder = next((f for f in folders if f["mode"] == "chat"), None)
        if folder is None:
            names = {f["normalized"] for f in folders}
            name, number = "ИИ", 2
            while name.casefold() in names:
                name, number = f"ИИ {number}", number + 1
            folder = self.store.create_folder(owner, name)
            self.ai_state.mode(owner, folder["id"], "chat")
        self.store.select(owner, folder["id"])
        self.store.set_action(owner, None)
        self.ai_state.active(owner, folder["id"])
        await self.show_bottom(owner, f"🤖 Папка «{folder['name']}» — диалог с ИИ.\n"
                                   "Отправь текст или голосовое. Можно попросить найти, создать или изменить заметку.",
                                   thread, keys([key("Новый разговор", f"ainew:{folder['id']}")],
                                   [key("История разговоров", f"ailist:{folder['id']}:0"), key("Настройки", "aisettings")]),
                                   screen=f"foldermenu:{folder['id']}", back="home")

    async def new_ai_conversation(self, owner, thread, folder_id):
        self.store.set_action(owner, None)
        folder = self.store.folder(owner, folder_id)
        conversation = self.ai_state.new_conversation(owner, folder_id)
        text = "Новый разговор. Предыдущая история сохранена."
        text += ("\nОтправь текст или голосовое." if folder["mode"] == "chat"
                 else "\nОтветь на это сообщение, чтобы начать. Обычные сообщения в этой папке остаются заметками.")
        response = await self.show(owner, text, thread)
        self.ai_state.link(owner, response["message_id"], conversation)

    async def ai_history(self, owner, thread, folder_id, page=0, edit_id=None):
        self.store.folder(owner, folder_id)
        page = max(0, page)
        history = self.store.all("SELECT a.note_id,n.transcript FROM ai_requests a "
                                 "JOIN ai_conversations c ON c.id=a.conversation_id JOIN notes n ON n.id=a.note_id "
                                 "WHERE a.owner=? AND c.folder_id=? AND n.deleted=0 "
                                 "ORDER BY a.created_at DESC LIMIT 9 OFFSET ?", (owner, folder_id, page * 8))
        rows = [[key("🤖 " + (n["transcript"] or "Голосовое обрабатывается")[:55], f"aiview:{n['note_id']}")]
                for n in history[:8]]
        nav = []
        if page:
            nav.append(key("←", f"ailist:{folder_id}:{page - 1}"))
        if len(history) > 8:
            nav.append(key("→", f"ailist:{folder_id}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([key("← К папке", f"folder:{folder_id}:0")])
        await self.show_bottom(owner, "История ИИ. Выбери сообщение:" if history else "Разговоров пока нет.",
                               thread, keys(*rows), screen=f"ailist:{folder_id}:{page}",
                               back=f"foldermenu:{folder_id}", edit_id=edit_id)

    async def ai_callback(self, owner, thread, message_id, data):
        parts = data.split(":")
        action, identity = parts[0], parts[1] if len(parts) > 1 else None
        if action == "aiplace" and len(parts) == 4:
            text = await self.ai_note_actions.choose(owner, identity, int(parts[2]), parts[3])
            await self.show(owner, text, thread, keys([key("Открыть ответ ИИ", f"aiview:{identity}")]), message_id)
        elif action == "aisettings":
            await self.ai_settings(owner, thread, message_id)
        elif action == "aiopen":
            await self.open_ai(owner, thread)
        elif action == "aiaccessmenu":
            selected = self.ai_state.settings(owner)["notes_access"]
            choices = [("none", "Выключен"), ("read", "Только чтение"), ("write", "Чтение и запись")]
            await self.show_bottom(owner, "Что ИИ может делать с твоими заметками?", thread, keys(
                *[[key(("✓ " if selected == value else "") + label, f"aiaccess:{value}")]
                  for value, label in choices]), screen="aiaccessmenu", back="aisettings", edit_id=message_id)
        elif action in {"aiwake", "aiaccess"}:
            self.ai_state.set_settings(owner, **({"wake_enabled": identity == "1"} if action == "aiwake"
                                                else {"notes_access": identity}))
            await self.ai_settings(owner, thread, message_id)
        elif action == "ainames":
            self.store.set_action(owner, "ai_names")
            await self.show_bottom(owner, "Пришли имена обращения через запятую, например: Луна, Астра, GPT.",
                                   thread, screen="ainames", back="aisettings", keep=True, edit_id=message_id)
        elif action == "aimodels":
            selected = self.ai_state.settings(owner)["model"] or self.config.llm.model
            await self.show_bottom(owner, "Выбери модель:", thread, keys(
                *[[key(("✓ " if model == selected else "") + model, f"aimodel:{index}")]
                  for index, model in enumerate(self.ai_models)]), screen="aimodels", back="aisettings",
                edit_id=message_id)
        elif action == "aimodel":
            if not identity or not identity.isdigit() or int(identity) >= len(self.ai_models):
                raise ValueError("Модель не найдена")
            self.ai_state.set_settings(owner, model=self.ai_models[int(identity)])
            await self.ai_settings(owner, thread, message_id)
        elif action == "aimode":
            self.ai_state.mode(owner, identity, parts[2])
            await self.show_notes_menu(owner, thread, identity, edit_id=message_id)
        elif action == "ainew":
            await self.new_ai_conversation(owner, thread, identity)
        elif action == "ailist":
            await self.ai_history(owner, thread, identity, int(parts[2]), message_id)
        elif action in {"aioptions", "aicollapse"}:
            request = self.ai_state.request(owner, identity)
            if not request:
                raise ValueError("Запрос не найден")
            rows, _ = self.ai_result_controls(owner, identity, request)
            await self.edit_note_buttons(owner, message_id, keys(*rows) if action == "aioptions"
                                         else keys(*self.ai_note_actions.buttons(owner, identity)))
        elif action in {"aiask", "aicontinue"}:
            if action == "aiask":
                conversation = self.ai_state.start_note_conversation(owner, identity)
                prompt = "Отправь инструкцию для ИИ текстом или голосовым. Можно добавить ссылку."
            else:
                request = self.ai_state.request(owner, identity)
                if not request or not request["response"]:
                    raise ValueError("Дождись ответа ИИ")
                conversation = request["conversation_id"]
                prompt = "Продолжаем общаться с ИИ. Отправь ещё одно сообщение."
            self.store.set_action(owner, f"ai_note:{conversation}")
            if action == "aicontinue":
                response = await self.view_send(self.telegram.text, owner, prompt, thread,
                    keys([key("Отменить", f"aicancel:{conversation}")]))
            else:
                response = await self.show_bottom(owner, prompt, thread, keys(),
                    screen=f"ai_note:{conversation}", back="root", keep=True)
            self.ai_state.link(owner, response["message_id"], conversation)
        elif action == "aicancel":
            if self.store.action(owner) == f"ai_note:{identity}":
                self.store.set_action(owner, None)
            await self.remove_menu_message(owner, message_id)
        elif action == "aiend":
            if self.store.action(owner) == f"ai_note:{identity}":
                self.store.set_action(owner, None)
            if message_id:
                await self.edit_note_buttons(owner, message_id, keys())
            await self.show_home(owner, thread)
        elif action in {"aiview", "airetry"}:
            request = self.ai_state.request(owner, identity)
            if not request:
                raise ValueError("Запрос не найден")
            if action == "airetry":
                if request["state"] != "failed":
                    raise ValueError("Запрос уже выполняется или завершён")
                self.ai_state.update_request(owner, identity, state="queued", error=None, response=None,
                                             delivery="pending", model=self.ai_engine.model(owner))
                await self.show(owner, "Повторю запрос. Уже выполненные изменения не будут повторяться.", thread)
            elif request["response"]:
                await self.deliver_ai(request, force=True, thread=thread)
            else:
                await self.show(owner, "Запрос ещё обрабатывается. Ответ появится здесь.", thread)
        elif action == "aiundo":
            if self.note_locks[identity].locked():
                raise ValueError("Дождись завершения обработки записи")
            self.ai_state.undo(owner, identity, int(parts[2]))
            self.flush_ai_archive()
            await self.show_note(owner, identity, thread)
        else:
            return False
        return True

    def flush_ai_archive(self):
        for note in self.store.all("SELECT * FROM notes WHERE archive_dirty=1 AND deleted=0 LIMIT 20"):
            self.archive.persist_text(note, self.store.folder(note["owner"], note["folder_id"]))
            self.store.update_note(note["owner"], note["id"], archive_dirty=0)

    async def deliver_ai(self, request, force=False, thread=None):
        if self.ai_delivery_locks[request["note_id"]].locked():
            return
        async with self.ai_delivery_locks[request["note_id"]]:
            await self._deliver_ai(request, force, thread)

    def ai_result_controls(self, owner, identity, current):
        rows, changes = [], []
        for receipt in self.ai_engine.receipts(owner, identity):
            label = {"update_note": "Изменено", "move_note": "Перенесено", "delete_note": "Удалено"}.get(
                receipt["action"], "Создано")
            changes.append(f"{label}: {receipt['title']}")
            if receipt.get("cleanup_state") in {"pending", "blocked"}:
                changes.append(receipt["message"])
            exists = self.store.one("SELECT id FROM notes WHERE owner=? AND id=? AND deleted=0", (owner, receipt['id']))
            if exists:
                rows.append([key(receipt["title"][:45], f"note:{receipt['id']}")])
            if exists and receipt["action"] == "update_note":
                rows[-1].append(key("Отменить правку", f"aiundo:{receipt['id']}:{receipt['revision']}"))
        if current["state"] == "failed":
            rows.append([key("Повторить запрос", f"airetry:{identity}")])
        context = self.ai_state.note_context(owner, current["conversation_id"])
        source = context['note_id'] if context else identity
        if self.store.one("SELECT id FROM notes WHERE owner=? AND id=? AND deleted=0", (owner, source)):
            rows.append([key("Исходное сообщение", f"note:{source}")])
        rows[:0] = self.ai_note_actions.buttons(owner, identity)
        rows.append([key("← Назад", f"aicollapse:{identity}")])
        return rows, changes

    async def show_ai_progress(self, request):
        owner, identity = request["owner"], request["note_id"]
        note = self.store.note(owner, identity)
        if (request["trigger"] == "miniapp" or note["kind"] != "voice"
                or request["state"] not in {"queued", "running"}):
            return
        # Checkpoint before sending: an ambiguous network failure must not create duplicates.
        with self.store.db:
            inserted = self.store.db.execute("INSERT OR IGNORE INTO ai_progress VALUES (?,0)", (identity,)).rowcount
        if not inserted:
            return
        try:
            sent = await self.telegram.text(owner, "🤖 Обрабатываю команду…", request["thread_id"],
                                            reply_to=note["transcript_message_id"])
            with self.store.db:
                self.store.db.execute("UPDATE ai_progress SET message_id=? WHERE note_id=?",
                                      (sent["message_id"], identity))
            self.ai_state.link(owner, sent["message_id"], request["conversation_id"])
        except ProviderError:
            log.warning("AI progress delivery unavailable request=%s", identity)

    async def _deliver_ai(self, request, force=False, thread=None):
        from .bot import transcript_chunks
        owner, identity = request["owner"], request["note_id"]
        current = self.ai_state.request(owner, identity)
        if not current or (not force and current["delivery"] != "pending") or not current["response"]:
            return
        if current["trigger"] == "miniapp" and not force:
            self.ai_state.update_request(owner, identity, delivery="sent")
            return
        self.ai_state.conversation(owner, current["conversation_id"])
        _, changes = self.ai_result_controls(owner, identity, current)
        text = "🤖 " + current["response"]
        if changes:
            text += "\n\n" + "\n".join(changes)
        if force:
            note = self.store.note(owner, identity)
            text = "Ты: " + (note["transcript"] or "") + "\n\n" + text
        note = self.store.note(owner, identity)
        reply_to = note["transcript_message_id"]
        controls = self.ai_note_actions.buttons(owner, identity)
        context = self.ai_state.note_context(owner, current["conversation_id"])
        context_deleted = context and not self.store.one(
            "SELECT id FROM notes WHERE owner=? AND id=? AND deleted=0", (owner, context["note_id"]))
        if context_deleted:
            controls.append([key("Папки", "folders")])
        if current["state"] == "failed":
            controls.append([key("Повторить запрос", f"airetry:{identity}")])
        chunks = list(transcript_chunks(text))
        progress = None if force else self.store.one("SELECT message_id FROM ai_progress WHERE note_id=?", (identity,))
        self.ai_state.update_request(owner, identity, delivery="sending")
        try:
            for index, chunk in enumerate(chunks):
                keyboard = keys(*controls) if index == len(chunks) - 1 else None
                sent = None
                if index == 0 and progress and progress["message_id"]:
                    try:
                        sent = await self.telegram.call("editMessageText", chat_id=owner,
                            message_id=progress["message_id"], text=chunk, reply_markup=keyboard or keys(),
                            link_preview_options={"is_disabled": True})
                    except ProviderError as exc:
                        if getattr(exc, "reason", None) == "not_modified":
                            sent = {"message_id": progress["message_id"]}
                        elif getattr(exc, "reason", None) != "edit_target_missing":
                            raise
                if sent is None:
                    sent = await self.telegram.text(owner, chunk, thread if force else current["thread_id"],
                        keyboard, reply_to=None if force else reply_to)
                self.ai_state.link(owner, sent["message_id"], current["conversation_id"])
                result = self.store.one("SELECT * FROM notes WHERE owner=? AND id=? AND deleted=0",
                                        (owner, current["result_note_id"]))
                if result:
                    self.messages.record(result, sent["message_id"], thread if force else current["thread_id"],
                                         "ai_response")
            self.ai_state.update_request(owner, identity, delivery="sent")
        except ProviderError:
            self.ai_state.update_request(owner, identity, delivery="unknown")
            log.warning("AI delivery outcome unknown request=%s", identity)

    async def process_ai(self, request):
        async with self.note_locks[request["note_id"]]:
            await self._process_ai(request)

    async def _process_ai(self, request):
        owner, identity = request["owner"], request["note_id"]
        try:
            await self.ai_engine.run(request)
        except (ProviderError, ValueError, OSError) as exc:
            note = self.store.one("SELECT deleted FROM notes WHERE id=? AND owner=?", (identity, owner))
            if not note or note["deleted"]:
                return
            text = (str(exc) if isinstance(exc, ValueError) else
                    "ИИ сейчас не смог завершить запрос. Текст сохранён; можно повторить.")
            self.ai_state.update_request(owner, identity, state="failed", error=type(exc).__name__, response=text)
            log.warning("AI request failed request=%s type=%s code=%s", identity, type(exc).__name__,
                        str(exc) if isinstance(exc, ProviderError) else "-")
        self.flush_ai_archive()
        await self.deliver_ai(self.ai_state.request(owner, identity))

    async def ai_worker(self):
        while True:
            if self.view_rendering:
                await asyncio.sleep(.2)
                continue
            try:
                await self.ai_note_actions.resume()
                self.flush_ai_archive()
                request = self.ai_state.claim()
                if request:
                    await self.process_ai(request)
                    continue
                ready = self.store.one("SELECT a.* FROM ai_requests a JOIN notes n ON n.id=a.note_id "
                                       "WHERE a.state IN ('ready','failed') AND a.delivery='pending' AND n.deleted=0 "
                                       "ORDER BY a.created_at LIMIT 1")
                if ready:
                    await self.deliver_ai(ready)
            except (OSError, ProviderError) as exc:
                log.warning("AI worker unavailable type=%s", type(exc).__name__)
                await asyncio.sleep(3)
            await asyncio.sleep(.5)
