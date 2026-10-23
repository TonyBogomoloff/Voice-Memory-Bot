from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

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
        self.ai_models = tuple(dict.fromkeys((self.config.llm.model, *self.config.assistant_models)))
        self.assistant = assistant or CodexAssistant(self.config.llm)
        self.ai_engine = AssistantEngine(self.ai_state, self.assistant, self.config.llm.model, self.ai_models, self.archive)
        self.ai_delivery_locks = defaultdict(asyncio.Lock)

    def check_ai_capacity(self, owner, folder_id, message):
        folder = self.store.folder(owner, folder_id)
        reply = self.ai_state.reply_conversation(owner, message.get("reply_to_message", {}).get("message_id"))
        if folder["mode"] == "chat" or reply:
            pending = self.store.one("SELECT count(*) AS n FROM ai_requests WHERE owner=? "
                                     "AND state IN ('queued','running')", (owner,))["n"]
            if pending >= self.store.limits.max_pending_per_user:
                raise ValueError("Очередь ИИ заполнена; дождись предыдущих ответов")

    def capture_ai_input(self, note, message):
        owner = note["owner"]
        conversation = self.ai_state.reply_conversation(owner, message.get("reply_to_message", {}).get("message_id"))
        if conversation or note["input_mode"] == "chat":
            trigger = "reply" if conversation else "folder"
            if not conversation and not self.ai_state.request(owner, note["id"]):
                conversation = self.ai_state.active(owner, note["source_folder"])
            self.ai_state.enqueue(owner, note["id"], trigger,
                                  message.get("message_thread_id"), message["message_id"], conversation)

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
        self.store.update_note(owner, identity, state="ready", routing="ai", requested_folder=None,
                               title=" ".join((note["transcript"] or "").split())[:80] or "Диалог с ИИ",
                               delivery=current["transcript_delivery"],
                               result_message_id=current["transcript_message_id"], result_thread=note["source_thread"])
        self.archive.persist_text(self.store.note(owner, identity), self.store.folder(owner, current["folder_id"]))
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
        rows.extend([[key("Доступ к заметкам", "aiaccessmenu")],
                     [key("Выбрать модель", "aimodels")], [key("Открыть ИИ", "aiopen"), key("Папки", "folders")]])
        text += "\n\nВ ИИ-папке все сообщения идут в диалог. Ответ на реплику ИИ продолжает разговор."
        text += "\nПри доступе к заметкам модель получает найденные записи по мере запроса."
        await self.show(owner, text, thread, keys(*rows), edit_id)

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
        target = await self.ensure_topic(owner, folder["id"])
        await self.ensure_record_panel(owner, target)
        conversation = self.ai_state.active(owner, folder["id"])
        response = await self.show(owner, f"🤖 Папка «{folder['name']}» — диалог с ИИ.\n"
                                   "Отправь текст или голосовое. Можно попросить найти, создать или изменить заметку.",
                                   target or thread, keys([key("Новый разговор", f"ainew:{folder['id']}")],
                                   [key("История разговоров", f"ailist:{folder['id']}:0"), key("Настройки", "aisettings")]))
        self.ai_state.link(owner, response["message_id"], conversation)

    async def new_ai_conversation(self, owner, thread, folder_id):
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
        rows = [[key((n["transcript"] or "Голосовое обрабатывается")[:55], f"aiview:{n['note_id']}")]
                for n in history[:8]]
        nav = []
        if page:
            nav.append(key("←", f"ailist:{folder_id}:{page - 1}"))
        if len(history) > 8:
            nav.append(key("→", f"ailist:{folder_id}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([key("← К папке", f"folder:{folder_id}:0")])
        await self.show(owner, "История ИИ. Выбери сообщение:" if history else "Разговоров пока нет.",
                        thread, keys(*rows), edit_id)

    async def ai_callback(self, owner, thread, message_id, data):
        parts = data.split(":")
        action, identity = parts[0], parts[1] if len(parts) > 1 else None
        if action == "aisettings":
            await self.ai_settings(owner, thread, message_id)
        elif action == "aiopen":
            await self.open_ai(owner, thread)
        elif action == "aiaccessmenu":
            selected = self.ai_state.settings(owner)["notes_access"]
            choices = [("none", "Выключен"), ("read", "Только чтение"), ("write", "Чтение и запись")]
            await self.show(owner, "Что ИИ может делать с твоими заметками?", thread, keys(
                *[[key(("✓ " if selected == value else "") + label, f"aiaccess:{value}")]
                  for value, label in choices], [key("← Настройки", "aisettings")]), message_id)
        elif action in {"aiwake", "aiaccess"}:
            self.ai_state.set_settings(owner, **({"wake_enabled": identity == "1"} if action == "aiwake"
                                                else {"notes_access": identity}))
            await self.ai_settings(owner, thread, message_id)
        elif action == "ainames":
            self.store.set_action(owner, "ai_names")
            await self.show(owner, "Пришли имена обращения через запятую, например: Луна, Астра, GPT. /cancel — отмена.", thread)
        elif action == "aimodels":
            await self.show(owner, "Выбери модель:", thread, keys(
                *[[key(model, f"aimodel:{index}")] for index, model in enumerate(self.ai_models)],
                [key("← Настройки", "aisettings")]), message_id)
        elif action == "aimodel":
            if not identity or not identity.isdigit() or int(identity) >= len(self.ai_models):
                raise ValueError("Модель не найдена")
            self.ai_state.set_settings(owner, model=self.ai_models[int(identity)])
            await self.ai_settings(owner, thread, message_id)
        elif action == "aimode":
            self.ai_state.mode(owner, identity, parts[2])
            await self.show_notes(owner, thread, identity, edit_id=message_id)
        elif action == "ainew":
            await self.new_ai_conversation(owner, thread, identity)
        elif action == "ailist":
            await self.ai_history(owner, thread, identity, int(parts[2]), message_id)
        elif action == "aiask":
            note = self.store.note(owner, identity)
            request = self.ai_state.enqueue(owner, identity, "button", thread, message_id)
            if note["state"] == "ready":
                # A request may be queued while classification is still finishing; route_ai rechecks it.
                self.store.update_note(owner, identity, routing="ai", requested_folder=None)
            await self.edit_note_buttons(owner, message_id, keys(
                [key("Открыть ответ ИИ" if request["response"] else "Запрос отправлен ИИ", f"aiview:{identity}")],
                [key("← Опции", f"options:{identity}")]))
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

    async def _deliver_ai(self, request, force=False, thread=None):
        from .bot import transcript_chunks
        owner, identity = request["owner"], request["note_id"]
        current = self.ai_state.request(owner, identity)
        if not current or (not force and current["delivery"] != "pending") or not current["response"]:
            return
        if current["trigger"] == "miniapp" and not force:
            self.ai_state.update_request(owner, identity, delivery="sent")
            return
        conversation = self.ai_state.conversation(owner, current["conversation_id"])
        rows, changes = [], []
        for receipt in self.ai_engine.receipts(owner, identity):
            label = "Изменено" if receipt["action"] == "update_note" else "Создано"
            changes.append(f"{label}: {receipt['title']}")
            rows.append([key(receipt["title"][:45], f"note:{receipt['id']}")])
            if receipt["action"] == "update_note":
                rows[-1].append(key("Отменить правку", f"aiundo:{receipt['id']}:{receipt['revision']}"))
        if current["state"] == "failed":
            rows.append([key("Повторить запрос", f"airetry:{identity}")])
        rows.append([key("Исходное сообщение", f"note:{identity}")])
        rows.append([key("Новый разговор", f"ainew:{conversation['folder_id']}"), key("Настройки ИИ", "aisettings")])
        text = "🤖 " + current["response"]
        if changes:
            text += "\n\n" + "\n".join(changes)
        if force:
            note = self.store.note(owner, identity)
            text = "Ты: " + (note["transcript"] or "") + "\n\n" + text
        chunks = list(transcript_chunks(text))
        self.ai_state.update_request(owner, identity, delivery="sending")
        try:
            for index, chunk in enumerate(chunks):
                sent = await self.telegram.text(owner, chunk, thread if force else current["thread_id"],
                    keys(*rows) if index == len(chunks) - 1 else None,
                    reply_to=None if force else current["reply_to"])
                self.ai_state.link(owner, sent["message_id"], current["conversation_id"])
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
            try:
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
