from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
import time
from collections import defaultdict, deque
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .ai_bot import AIBotMixin
from .archive import Archive, is_audio
from .bottom_menu import BottomMenuMixin
from .cleanup import CleanupMixin
from .config import Config
from .custom_emoji import KeyboardAliases
from .folder_menu import MENU_COMMANDS, OPTION_ACTIONS, FolderMenuMixin
from .folder_naming import FolderNamingMixin
from .folder_view import FolderViewMixin
from .message_store import MessageStore
from .providers import CodexRouter, Groq, ProviderError, Telegram
from .reply_move import ReplyMoveMixin
from .routing import fallback_title
from .store import Store, clean_name

log = logging.getLogger(__name__)


def button(text, data):
    return {"text": text, "callback_data": data, **(
        {"style": "danger"} if data == "deleteall" or data.startswith("confirmdeleteall:") else {})}


def keyboard(*rows):
    return {"inline_keyboard": list(rows)}


def transcript_chunks(text, limit=3900):
    """Keep the entire transcript, with room for Telegram's UTF-16 length limit."""
    start, units = 0, 0
    for index, char in enumerate(text):
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit:
            yield text[start:index]
            start, units = index, 0
        units += size
    if start < len(text):
        yield text[start:]


HOME = keyboard([button("📂 Все папки", "folders"), button("＋ Новая папка", "create")],
                [button("📝 Записи", "notes"), button("🔎 Поиск", "search")],
                [button("🤖 ИИ", "aiopen"), button("📦 Экспорт всего", "export:all")],
                [button("⚙️ Настройки", "settings:all"), button("ℹ️ Помощь", "help")],
                [button("🗑 Удалить всё", "deleteall")])

HELP = """🎙 Voice Memory Bot

/ai — изменить запись с ИИ.
/move — перенести запись в другую папку.
/delete — удалить запись из базы и чата без подтверждения.
Без ответа на сообщение команды выбирают последнюю твою запись в открытой папке.

Выбери папку внизу — новые голосовые, тексты и фото сохраняются туда.
Активная папка отмечена ✅. Стрелки листают страницы папок.
«＋» создаёт папку: напиши название или пришли голосовое.
«📁 Папка» — переименовать, удалить или экспортировать выбранную папку.
«Назад» из меню папки возвращает на главную.
При удалении папки записи остаются в архиве; бот сначала попросит подтверждение.

В «🎛️ Меню» можно включить кнопки под сообщениями, открыть Help или поговорить с ИИ.
Кнопки «Переместить», «Удалить», «ИИ» по умолчанию выключены.
Архив и расшифровка работают и без кнопок. Голосовые и видеокружки расшифровываются в текст.

«Поговорить с ИИ» открывает папку для диалога. Отправь текст или голосовое.
ИИ умеет искать, создавать, менять и переносить заметки, а также удалять их с подтверждением.

/start — главная
/help — помощь
/ai — поговорить с ИИ"""


class Bot(ReplyMoveMixin, BottomMenuMixin, FolderNamingMixin, FolderViewMixin, FolderMenuMixin, CleanupMixin, AIBotMixin):
    def __init__(self, config: Config, store: Store, telegram: Telegram, groq: Groq, router: CodexRouter,
                 assistant=None):
        self.config, self.store = config, store
        self.telegram, self.groq, self.router = telegram, groq, router
        self.archive = Archive(config.data_dir / "notes")
        self.messages = MessageStore(store)
        self.recent = defaultdict(deque)
        self.export_locks = defaultdict(asyncio.Lock)
        self.note_locks = defaultdict(asyncio.Lock)
        self.export_tasks = set()
        self.init_ai(assistant)
        self.init_cleanup()
        self.folder_delete_confirmations = {}
        self.folder_keyboards = {}
        self.folder_pages = {}
        self.init_folder_options()
        self.init_folder_view()
        self.init_bottom_menu()
        self.emoji_keyboards = KeyboardAliases(store)
        self.telegram.on_keyboard = self.emoji_keyboards.remember
        self.init_reply_moves()
        self.folder_namer = CodexRouter(replace(config.llm, model=config.folder_name_model or config.llm.model, effort="low"))
        self.init_folder_naming()

    async def start(self):
        me = await self.telegram.call("getMe")
        webhook = await self.telegram.call("getWebhookInfo")
        if webhook.get("url"):
            raise ProviderError("webhook_present_stop_before_polling")
        await self.telegram.call("setMyCommands", commands=[
            {"command": "ai", "description": "Изменить с ИИ"},
            {"command": "move", "description": "Перенести запись"},
            {"command": "delete", "description": "Удалить запись"},
            {"command": "options", "description": "Опции"},
        ])
        # Finish physical deletion interrupted by a previous shutdown.
        for note in self.store.all("SELECT * FROM notes WHERE deleted=1"):
            self.archive.delete(note)
        await self.resume_deletions()
        await self.configure_menu()
        log.info("started bot_id=%s folder_mode=custom telegram_topics=%s", me["id"], me.get("has_topics_enabled", False))
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self.poll())
                group.create_task(self.dispatch())
                group.create_task(self.worker())
                group.create_task(self.ai_worker())
                group.create_task(self.folder_view_worker())
        finally:
            for task in self.export_tasks:
                task.cancel()
            await asyncio.gather(*self.export_tasks, return_exceptions=True)

    async def poll(self):
        while True:
            try:
                capacity = self.store.updates.available()
                if not capacity:
                    await asyncio.sleep(2)
                    continue
                updates = await self.telegram.call("getUpdates", offset=self.store.offset(), timeout=30,
                                                   allowed_updates=["message", "edited_message", "callback_query"],
                                                   limit=min(50, capacity))
                self.store.ingest(updates, self.config.allowed_user_ids)
            except ProviderError as exc:
                if str(exc) in {"telegram_401", "telegram_409"}:
                    raise
                log.warning("poll error=%s", exc)
                await asyncio.sleep(3)

    def rate_allowed(self, owner):
        now, queue = time.monotonic(), self.recent[owner]
        while queue and queue[0] < now - 60:
            queue.popleft()
        if len(queue) >= 30:
            return False
        queue.append(now)
        return True

    async def dispatch(self):
        while True:
            if not await self.dispatch_next():
                await asyncio.sleep(.2)

    async def dispatch_next(self):
        update = self.store.pending_update()
        if update is None:
            return False
        selected = [update]
        completed = None
        try:
            selected = self.coalesce_folder_updates(update)
            if len(selected) > 1:
                log.info('folder navigation coalesced count=%s', len(selected))
            for skipped in selected[:-1]:
                message = skipped['message']
                self.cleanup.remember(message['from']['id'], message['message_id'],
                                      message.get('message_thread_id'), message.get('date'))
            update = selected[-1]
            await self.handle(update)
            await self.remove_coalesced_taps(selected)
            completed = True
        except ProviderError as exc:
            completed = True
            # UI sends are not retried automatically: a lost response may already have delivered.
            log.warning("update failed update_id=%s error=%s method=%s reason=%s", update["update_id"], exc,
                        getattr(exc, "method", "unknown"), getattr(exc, "reason", "unknown"))
        except Exception as exc:
            completed = False
            log.error("update failed update_id=%s type=%s", update["update_id"], type(exc).__name__)
        finally:
            for item in selected:
                if completed is True:
                    self.store.finish_update(item["update_id"])
                elif completed is False:
                    self.store.fail_update(item["update_id"])
        return True

    async def show(self, owner, text, thread=None, keys=None, edit_id=None):
        if keys and "inline_keyboard" in keys:
            action = self.store.action(owner) or ""
            if action.startswith("ai_note:"):
                return await self.show_bottom(owner, text, thread, screen=action, keep=True)
            state = self.bottom_state(owner) or {}
            return await self.show_bottom(owner, text, thread, keys, screen="notice",
                                          back=state.get("back" if state.get("screen") == "notice" else "screen", "home"),
                                          keep=True, edit_id=edit_id)
        result = await self._show(owner, text, thread, keys, edit_id)
        self.remember_screen(owner, thread, text, keys)
        return result

    async def _show(self, owner, text, thread=None, keys=None, edit_id=None):
        if edit_id:
            try:
                return await self.telegram.call("editMessageText", chat_id=owner, message_id=edit_id,
                                               text=text[:4000], reply_markup=keys,
                                               link_preview_options={"is_disabled": True})
            except ProviderError as exc:
                if getattr(exc, "reason", None) == "not_modified":
                    return {"message_id": edit_id}
                if str(exc) != "telegram_400":
                    raise
                # Captions cannot be edited as text; older messages may no longer be editable.
        return await self.telegram.text(owner, text, thread, keys)

    async def handle(self, update):
        callback = update.get("callback_query")
        sender = (callback or update.get("message") or update.get("edited_message", {})).get("from", {})
        owner = sender.get("id")
        async with self.view_locks[owner]:
            message = update.get("message") or {}
            pending = {"message_id": message.get("message_id"), "remove": False, "tasks": []}
            self.command_cleanups[owner] = pending
            try:
                result = await self._handle(update)
            except BaseException:
                # Keep the user's command when the response could not be delivered.
                self.command_cleanups.pop(owner, None)
                raise
            self.command_cleanups.pop(owner, None)
            if pending["remove"]:
                if pending["tasks"]:
                    task = asyncio.create_task(self.finish_command_cleanup(owner, pending))
                    self.export_tasks.add(task)
                    task.add_done_callback(self.export_tasks.discard)
                else:
                    await self.remove_menu_message(owner, pending["message_id"])
            return result

    async def finish_command_cleanup(self, owner, pending):
        await asyncio.gather(*pending["tasks"])
        await self.remove_menu_message(owner, pending["message_id"])

    async def _handle(self, update):
        callback = update.get("callback_query")
        message = update.get("message") or update.get("edited_message") or (callback or {}).get("message", {})
        sender = callback.get("from", {}) if callback else message.get("from", {})
        owner = sender.get("id")
        if owner not in self.config.allowed_user_ids or message.get("chat", {}).get("type") != "private":
            return
        if message["chat"]["id"] != owner:
            return
        if "edited_message" in update:
            note = self.store.one("SELECT * FROM notes WHERE owner=? AND message_id=? AND deleted=0",
                                  (owner, message.get("message_id")))
            if note:
                async with self.note_locks[note["id"]]:
                    self.store.edit_source(owner, message)
            return
        thread = message.get("message_thread_id")
        self.cleanup.remember(owner, message.get("message_id"), thread, message.get("date"))
        move_callback = callback and callback.get("data", "").startswith(("place:", "move:", "newfor:"))
        if callback and not move_callback:
            await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"])
        navigation = message.get("text", "").strip()
        alias_active = None
        if not callback:
            navigation, alias_active = self.emoji_keyboards.resolve(owner, navigation)
        if (callback or navigation.startswith("/") or navigation in MENU_COMMANDS
                or navigation in OPTION_ACTIONS or navigation in {"⬅️", "➡️"} or self.is_bottom_control(owner, navigation)
                or self.is_folder_button(navigation)) and not self.rate_allowed(owner):
            if move_callback:
                await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"])
            return
        job = self.cleanup.job(owner)
        if owner in self.deleting_owners or job and job["files_pending"]:
            if owner not in self.deleting_owners and (
                callback and callback.get("data") == "retrydeleteall"
                or message.get("text", "").split("@", 1)[0] == "/delete_all"
                or (self.bottom_state(owner) or {}).get("actions", {}).get(navigation) == "retrydeleteall"
            ):
                return await self.resume_delete_all(owner)
            return await self.show(owner, "Очистка ещё не завершена. Повтори её перед отправкой новых заметок.",
                                   keys=keyboard([button("Повторить очистку", "retrydeleteall")]))
        self.store.register_user(owner)
        try:
            if callback:
                result = await self.callback(owner, thread, message["message_id"], callback.get("data", ""))
                if move_callback:
                    await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"], text=result)
                return result
            if "forum_topic_created" in message or "forum_topic_edited" in message:
                return
            if alias_active is False:
                await self.remove_menu_message(owner, message["message_id"])
                return await self.refresh_folder_keyboard(owner, thread, force=True,
                    notice="Эта кнопка устарела. Выбери действие внизу.")
            text = navigation
            if text.startswith("/"):
                await self.remove_menu_message(owner, message["message_id"])
            if text and text.split()[0].split("@", 1)[0].lower() == "/options":
                return await self.record_options(owner, thread, message)
            if text and text.split()[0].split("@", 1)[0].lower() == "/delete":
                return await self.delete_reply(owner, thread, message)
            if text and text.split()[0].split("@", 1)[0].lower() == "/move":
                return await self.move_reply(owner, thread, message)
            if text and text.split()[0].split("@", 1)[0].lower() == "/ai":
                note = self.reply_note(owner, message)
                if not note:
                    return await self._show(owner, "Не нашёл запись. Пришли сообщение или ответь на сохранённую запись.", thread)
                return await self.ai_callback(owner, thread, None, f"aiask:{note['id']}")
            if text == "➖":
                return await self.refresh_folder_keyboard(owner, thread, force=True,
                    notice="Для удаления выбери «Ответить» на запись и команду /delete в меню.")
            if await self.bottom_input(owner, thread, text, message["message_id"]):
                return
            # Folder taps remain as view anchors until the next selection.
            if text in MENU_COMMANDS or text in OPTION_ACTIONS or text in {"⬅️", "➡️"} or text.startswith("/"):
                await self.remove_menu_message(owner, message["message_id"])
            if text in OPTION_ACTIONS:
                return await self.folder_option_button(owner, thread, text)
            if text in {"⬅️", "➡️"}:
                return await self.turn_folder_page(owner, thread, text)
            if text in MENU_COMMANDS:
                return await self.command(owner, thread, MENU_COMMANDS[text], "")
            if self.is_folder_button(text):
                return await self.select_folder_button(owner, thread, text, message["message_id"])
            if text.startswith("/"):
                command, _, argument = text.partition(" ")
                return await self.command(owner, thread, command.split("@")[0].lower(), argument.strip())
            if text and (self.store.action(owner) or "").startswith("replymove:"):
                return await self.input_action(owner, thread, text)
            if (not self.reply_input(owner, message) and self.store.action(owner)
                    and not self.store.action(owner).startswith("ai_note:")):
                if text:
                    return await self.input_action(owner, thread, text)
                naming_action = self.store.action(owner)
                if naming_action == "create" or naming_action.startswith("rename:"):
                    if any(message.get(k) for k in ("voice", "audio", "video_note")):
                        rename_id = naming_action.split(":", 1)[1] if naming_action.startswith("rename:") else None
                        return await self.create_folder_from_voice(owner, thread, message, rename_id=rename_id)
                if naming_action == "create" or naming_action.startswith("rename:"):
                    return await self.show(owner, "Напиши название папки или пришли голосовое.", thread,
                                           {"inline_keyboard": []})
                return await self.show(owner, "Сначала пришли ответ текстом или нажми «Отмена».", thread,
                                       keyboard([button("Отмена", "home")]))
            if not self.store.folders(owner):
                return await self.ask_first_folder(owner, thread)
            folder = self.source_folder(owner, thread, message)["id"]
            self.check_ai_capacity(owner, folder, message)
            if text:
                note, created = self.store.add_note(owner, folder, message, "text", transcript=message["text"])
            else:
                kind = next((k for k in ("voice", "video_note", "video", "audio", "document", "photo") if message.get(k)), None)
                if kind is None:
                    return await self.show(owner, "Пришли голосовое, кружок, видео, файл, фото или текстовую заметку.", thread, HOME)
                media = message[kind][-1] if kind == "photo" else message[kind]
                extension = ("jpg" if kind == "photo" else "mp4" if kind in {"video_note", "video"} else "ogg" if kind == "voice"
                             else Path(media.get("file_name", "")).suffix.lower().lstrip("."))
                audio_extensions = {"ogg", "mp3", "mp4", "mpeg", "mpga", "m4a", "flac", "wav", "webm"}
                if kind == "document" and extension not in audio_extensions:
                    kind = "file"
                    extension = extension if extension.isascii() and extension.isalnum() and len(extension) <= 12 else "bin"
                if kind not in {"photo", "file"} and extension not in audio_extensions:
                    raise ValueError("Поддерживаются OGG, MP3, MP4, M4A, FLAC, WAV и WebM")
                size, duration = media.get("file_size", 0), media.get("duration", 0)
                if size <= 0 or size > self.config.limits.max_file_bytes:
                    raise ValueError(f"Пришли файл размером до {self.config.limits.max_file_bytes // 1_000_000} MB")
                if duration > self.config.limits.max_audio_seconds:
                    raise ValueError(f"Максимальная длительность — {self.config.limits.max_audio_seconds // 60} минут")
                # Uploaded documents lack reliable duration; probe the saved file before calling Groq.
                note, created = self.store.add_note(owner, folder, message, kind, media["file_id"],
                                                    extension, size, duration,
                                                    transcript=message.get("caption", "") if kind in {"photo", "file", "video"} else None)
            if created:
                self.messages.record(note, message["message_id"], thread, "source")
                # The worker uses the same lock, so a quick ASR cannot outrun the status checkpoint.
                async with self.note_locks[note["id"]]:
                    if note["kind"] == "photo":
                        if self.reply_input(owner, message):
                            self.capture_ai_input(note, message)
                        return
                    if note["kind"] == "text":
                        self.archive.persist_text(note, self.store.folder(owner, note["folder_id"]))
                        self.capture_ai_input(note, message, scoped=True)
                        await self.deliver_transcript(note)
                        request = self.ai_state.request(owner, note["id"])
                        if request:
                            await self.show_ai_progress(request)
                        return
                    status = "Конвертирую в текст…" if note["file_id"] and note['kind'] not in {"file", "video"} else "Сохраняю…"
                    # Editing this status preserves its reply linkage in Telegram.
                    # Keep it standalone from the start, including outside topics.
                    response = await self.telegram.text(owner, status, thread)
                    self.store.update_note(owner, note["id"], status_message_id=response["message_id"])
                    note = self.adopt_source_topic(note, response)
                    if note['kind'] not in {'file', 'video'}:
                        self.capture_ai_input(note, {**message, "message_thread_id": note["source_thread"]}, scoped=True)
                    log.info("note accepted note_id=%s source_thread=%s", note["id"], note["source_thread"])
        except ValueError as exc:
            if move_callback:
                return await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"],
                                                text=str(exc)[:200], show_alert=True)
            await self.show(owner, str(exc), thread, HOME)

    def source_folder(self, owner, thread, message=None):
        return self.store.folder(owner, self.store.current_folder(owner))

    def adopt_source_topic(self, note, response):
        thread = response.get("message_thread_id")
        if type(thread) is not int or thread <= 0:
            return self.store.note(note["owner"], note["id"])
        if note["source_thread"] is not None:
            if thread != note["source_thread"]:
                raise ProviderError("telegram_topic_mismatch")
            return self.store.note(note["owner"], note["id"])
        # Recover only the transport address; keep the folder selected at acceptance.
        note = self.messages.bind_source_thread(note, thread)
        self.cleanup.remember(note["owner"], note["message_id"], thread)
        if note["status_message_id"]:
            self.cleanup.remember(note["owner"], note["status_message_id"], thread)
        log.info("source topic resolved note_id=%s source_thread=%s", note["id"], thread)
        return note

    async def command(self, owner, thread, command, argument):
        self.store.set_action(owner, None)
        self.folder_delete_confirmations.pop(owner, None)
        if command == "/delete_all":
            return await self.delete_all_menu(owner, thread)
        if command == "/ai":
            return await self.open_ai(owner, thread)
        if not self.store.folders(owner) and command in {"/start", "/menu", "/folders", "/notes", "/newchat", "/cancel"}:
            return await self.ask_first_folder(owner, thread)
        if command == "/newchat":
            return await self.new_ai_conversation(owner, thread, self.store.current_folder(owner, thread))
        if command == "/start":
            return await self.show_home(owner, thread)
        if command == "/menu":
            return await self.show_main_menu(owner, thread)
        if command == "/options":
            return await self.show_folder_options(owner, thread)
        if command in {"/help", "/privacy"}:
            return await self.show_bottom(owner, HELP, thread, screen="help", back="home", keep=True)
        if command == "/cancel":
            self.delete_confirmations.pop(owner, None)
            return await self.show_home(owner, thread)
        if command == "/folders":
            return await self.show_folders(owner, thread)
        if command == "/new":
            if argument:
                return await self.create_folder(owner, thread, argument)
            self.store.set_action(owner, "create")
            return await self.show_bottom(owner, "Как назовём папку? Напиши название или пришли голосовое.",
                                          thread, screen="create", back="root", keep=True)
        if command == "/notes":
            return await self.show_notes(owner, thread, self.store.current_folder(owner, thread))
        if command == "/search":
            if argument:
                return await self.search(owner, thread, argument)
            self.store.set_action(owner, "search")
            return await self.show_bottom(owner, "Что найти в твоих заметках? Пришли слова текстом.",
                                          thread, screen="search", back="home", keep=True)
        if command == "/export":
            return await self.export_menu(owner, thread, "all")
        if command == "/settings":
            return await self.settings_menu(owner, thread)
        return await self.show(owner, "Команда не найдена. Открой меню папок или отправь заметку.", thread, HOME)

    async def input_action(self, owner, thread, text):
        action = self.store.action(owner)
        if action and action.startswith("replymove:"):
            return await self.reply_move_new_folder(owner, thread, action.split(":", 1)[1], text)
        if action == "ai_names":
            self.ai_state.set_settings(owner, names=[n.strip() for n in text.split(",") if n.strip()])
            self.store.set_action(owner, None)
            await self.ai_settings(owner, thread)
        elif action == "create":
            await self.create_folder(owner, thread, text)
        elif action == "search":
            await self.search(owner, thread, text)
        elif action.startswith("rename:"):
            identity = action.split(":")[1]
            folder = self.store.folder(owner, identity)
            name = clean_name(text)
            self.store.check_names(owner, [name, *json.loads(folder["aliases"])], identity)
            self.store.rename_folder(owner, identity, name)
            if self.options_folder(owner) == identity:
                await self.show_folder_options(owner, thread, identity)
            else:
                await self.show_notes(owner, thread, identity)
        elif action.startswith("aliases:"):
            identity = action.split(":")[1]
            aliases = [] if text == "-" else [s.strip() for s in text.split(",") if s.strip()]
            self.store.aliases(owner, identity, aliases)
            await self.show_notes(owner, thread, identity)
        self.store.set_action(owner, None)

    async def create_folder(self, owner, thread, name, note_id=None):
        folder = self.store.create_folder(owner, name)
        if note_id:
            return await self.move(owner, note_id, folder["id"], thread)
        await self.show_notes(owner, thread, folder["id"], notice=f"✅ Папка «{folder['name']}» создана.",
                              progress_text="Создаю папку…")

    async def show_folders(self, owner, thread=None, edit_id=None, page=0, note_id=None):
        if note_id:
            note = self.store.note(owner, note_id)
            return await self.move_reply(owner, thread, {
                "message_id": 0, "reply_to_message": {"message_id": note["message_id"]}})
        self.leave_folder_options(owner)
        if not self.store.folders(owner):
            return await self.ask_first_folder(owner, thread)
        self.store.set_action(owner, None)
        folders = self.store.folders(owner)
        active = self.store.current_folder(owner)
        rows = []
        page = max(0, min(page, (len(folders) - 1) // 4))
        for folder in folders[page * 4:(page + 1) * 4]:
            action = f"folder:{folder['id']}:0"
            marker = "✓" if folder["id"] == active else "📁"
            rows.append([button(f"{marker} {folder['name']} · {folder['count']}", action)])
        nav = []
        mode = "folders"
        if page:
            nav.append(button("⬅️ Папки", f"{mode}:{page - 1}"))
        if len(folders) > (page + 1) * 4:
            nav.append(button("Папки ➡️", f"{mode}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([button("＋ Новая папка", "create"), button("🔎 Поиск", "search")])
        rows.append([button("⌂ Меню", "home")])
        name = self.store.folder(owner, active)["name"]
        text = (
            f"📁 Все папки · {page + 1}/{(len(folders) - 1) // 4 + 1}\nСейчас сохраняю в «{name}».\n"
            "Открой папку, чтобы выбрать её для новых записей.")
        await self.show_bottom(owner, text, thread, keyboard(*rows), screen=f"folders:{page}",
                               back="home", edit_id=edit_id)

    async def show_notes_menu(self, owner, thread, identity, page=0, edit_id=None):
        folder = self.enter_folder_options(owner, identity)
        text = f"📁 {folder['name']}"
        text += "\nРежим: " + ("диалог с ИИ" if folder["mode"] == "chat" else "заметки")
        text += "\n✅ Новые записи сохраняются сюда. Пришли голосовое, текст или фото."
        rows = [[button("✏️ Переименовать", f"rename:{identity}"),
                 button("🗑 Удалить папку", f"dropfolder:{identity}")],
                [button("📦 Экспорт", f"zip:{identity}:all")]]
        await self.show_bottom(owner, text, thread, keyboard(*rows), screen=f"foldermenu:{identity}",
                               back="root", edit_id=edit_id)

    def note_card(self, note):
        folder = self.store.folder(note["owner"], note["folder_id"])
        date = datetime.fromtimestamp(note["created_at"], UTC).strftime("%d.%m.%Y %H:%M UTC")
        label = f"Без папки · {folder['name']}" if folder.get('removed') else folder['name']
        text = f"📁 {label}\n{note['title']}\n{date}"
        if note["state"] in {"queued", "processing"}:
            text += "\n⏳ Обрабатываю запись."
        elif note["state"] == "asr_failed":
            if note["kind"] in {"photo", "file", "video"}:
                text += "\nНе удалось сохранить файл. Попробуй ещё раз."
            else:
                text += "\nНе удалось получить расшифровку."
                text += " Аудио сохранено." if note["audio_bytes"] else " Загрузка аудио пока не завершена."
            if note["error"] == "daily_asr_limit":
                text += " Достигнут лимит распознавания за последние 24 часа."
            elif note["error"] == "asr_file_too_large":
                text += " Файл слишком большой для распознавания; оригинал доступен в выгрузке исходников."
        if note["routing"] == "unavailable":
            text += "\nВыбор проекта недоступен. Сохранил во «Входящие» — папку можно выбрать вручную."
        elif note["routing"] == "review":
            text += "\nУточни папку кнопкой ниже."
        content = note["content"] if note.get("content") is not None else note["transcript"]
        if content:
            text += "\n\n" + content[:450]
            if len(content) > 450:
                text += "…"
        identity = note["id"]
        rows = [[button("📄 Текст", f"text:{identity}"), button("📁 Переместить", f"choose:{identity}:0")]]
        if note["audio_bytes"]:
            label = ("📷 Фото" if note['kind'] == 'photo' else "📹 Кружок" if note['kind'] == 'video_note'
                     else "🎙 Аудио" if is_audio(note) else "📎 Файл")
            rows.append([button(label, f"audio:{identity}")])
        if note.get("content") is not None:
            rows.append([button("Исходная расшифровка", f"original:{identity}"),
                         button("Отменить правку", f"aiundo:{identity}:{note['revision']}")])
        rows.append([button("ИИ", f"aiask:{identity}")])
        if note["state"] == "asr_failed" or note["routing"] in {"unavailable", "review"}:
            rows.append([button("Повторить обработку", f"retry:{identity}")])
        if note["requested_folder"] and note["routing"] == "review":
            rows.append([button(f"＋ Создать «{note['requested_folder'][:35]}»", f"newfor:{identity}")])
        rows.append([button("Удалить из архива", f"delete:{identity}"), button("Папки", "folders")])
        return text[:1000], keyboard(*rows)

    async def show_note(self, owner, identity, thread, edit_id=None):
        note = self.store.note(owner, identity)
        if note["kind"] == "photo" and note["file_id"]:
            response = await self.telegram.call("sendPhoto", chat_id=owner, photo=note["file_id"],
                message_thread_id=thread, reply_markup=self.note_controls(note, thread))
            self.messages.record(note, response["message_id"], thread, "view")
            return
        content = note.get("content") if note.get("content") is not None else note["transcript"]
        answer = self.ai_state.result_request(owner, identity)
        if answer:
            content = "🤖 " + (content or "")
        for chunk in transcript_chunks(content or note["title"] or "Запись без расшифровки"):
            response = await self._show(owner, chunk, thread, self.note_controls(note, thread))
            self.messages.record(note, response["message_id"], thread, "view")
            if answer:
                self.ai_state.link(owner, response["message_id"], answer["conversation_id"])

    async def edit_note_buttons(self, owner, message_id, keys):
        try:
            await self.telegram.call("editMessageReplyMarkup", chat_id=owner, message_id=message_id,
                                     reply_markup=keys)
        except ProviderError as exc:
            if getattr(exc, "reason", None) != "not_modified":
                raise
        receipt = self.store.one("SELECT note_id,thread_id FROM note_messages "
                                 "WHERE owner=? AND message_id=?", (owner, message_id))
        if receipt:
            note = self.store.note(owner, receipt['note_id'])
            text, _ = self.note_card(note)
            self.remember_screen(owner, receipt['thread_id'], text, keys)

    async def choose_note_folder(self, owner, identity, message_id, page):
        note = self.store.note(owner, identity)
        return await self.move_reply(owner, None, {
            "message_id": 0, "reply_to_message": {"message_id": note["message_id"]}})

    async def callback(self, owner, thread, message_id, data):
        if await self.reply_move_callback(owner, thread, message_id, data):
            return
        if data.split(":", 1)[0] not in {"aioptions", "aicollapse", "aiend", "aicancel"}:
            self.store.set_action(owner, None)
        if await self.folder_menu_callback(owner, thread, message_id, data):
            return
        if await self.cleanup_callback(owner, thread, message_id, data):
            return
        if await self.ai_callback(owner, thread, message_id, data):
            return
        pieces = data.split(":")
        action, identity = pieces[0], pieces[1] if len(pieces) > 1 else None
        if action in {"record", "recordlink"}:
            # Retired keyboards lead to custom folders, never reconnect Telegram topics.
            return await self.show_folders(owner, thread)
        if action == "help":
            return await self.show_bottom(owner, HELP, thread, screen="help", back="home", keep=True,
                                          edit_id=message_id)
        if action in {"create", "search"}:
            return await self.command(owner, thread, "/new" if action == "create" else "/search", "")
        if action == "folders":
            return await self.show_folders(owner, thread, message_id, int(identity or 0))
        if action == "notes":
            return await self.show_notes(owner, thread, self.store.current_folder(owner), edit_id=message_id)
        if action == "folder":
            return await self.show_notes(owner, thread, identity, int(pieces[2]), message_id)
        if action == "select":
            return await self.show_notes(owner, thread, identity, edit_id=message_id)
        if action in {"rename", "aliases"}:
            self.store.folder(owner, identity)
            self.store.set_action(owner, data)
            prompt = "Напиши новое название или пришли голосовое — я выделю из него название папки." if action == "rename" else "Пришли названия через запятую. «-» удалит псевдонимы."
            return await self.show_bottom(owner, prompt, thread, screen=data, back=f"foldermenu:{identity}",
                                          keep=True, edit_id=message_id)
        if action == "export":
            return await self.export_menu(owner, thread, identity, message_id)
        if action == "settings":
            return await self.settings_menu(owner, thread, message_id)
        if action in {"messagebuttons", "menumessagebuttons"}:
            if identity not in {"0", "1"}:
                raise ValueError("Открой настройки заново")
            self.store.set_message_buttons(owner, identity == "1")
            if action == "menumessagebuttons":
                notice = "Кнопки под сообщениями " + ("включены." if identity == "1" else "выключены.")
                return await self.show_main_menu(owner, thread, message_id, notice=notice)
            return await self.settings_menu(owner, thread, message_id)
        if action == "exportsettings":
            return await self.export_settings(owner, thread, identity or "all", message_id)
        if action == "exportformat":
            scope = pieces[2] if len(pieces) > 2 else "all"
            if scope != "all":
                self.store.folder(owner, scope)
            self.store.set_export_format(owner, identity)
            return await self.export_settings(owner, thread, scope, message_id)
        if action == "zip":
            if len(pieces) != 3 or pieces[2] not in {"all", "text", "voice", "audio", "raw"}:
                raise ValueError("Выбери вид экспорта в меню /export")
            folder_id = None if identity == "all" else identity
            if folder_id:
                self.store.folder(owner, folder_id)
            # Keep old 'texts + audio' buttons working after the update.
            content = "all" if pieces[2] == "audio" else pieces[2]
            task = asyncio.create_task(self.send_export(owner, thread, folder_id, content))
            pending = self.command_cleanups.get(owner)
            if pending:
                pending["tasks"].append(task)
            self.export_tasks.add(task)
            task.add_done_callback(self.export_tasks.discard)
            task.add_done_callback(lambda _: self.view_refresh_event.set())
            return
        note = self.store.note(owner, identity)
        if action == "recordmove":
            return await self.move_reply(owner, thread, {
                "message_id": 0, "reply_to_message": {"message_id": note["message_id"]}})
        if action == "options":
            return await self.edit_note_buttons(owner, message_id, self.note_controls(note, thread))
        if action == "collapse":
            return await self.edit_note_buttons(owner, message_id, self.note_controls(note, thread))
        if action == "pick":
            return await self.choose_note_folder(owner, identity, message_id, int(pieces[2]))
        if action == "place":
            return await self.move(owner, identity, pieces[2], thread)
        if action == "note":
            return await self.show_note(owner, identity, thread, message_id)
        if action == "choose":
            return await self.show_folders(owner, thread, message_id, int(pieces[2]), identity)
        if action == "move":
            return await self.move(owner, identity, pieces[2], thread)
        if action == "newfor":
            if not note["requested_folder"] or note["routing"] != "review":
                raise ValueError("Новая папка для этой записи не предложена")
            return await self.create_folder(owner, thread, note["requested_folder"], identity)
        if action == "retry":
            self.store.retry(owner, identity)
            if note["kind"] == "photo":
                self.store.update_note(owner, identity, transcript_delivery="pending")
            async with self.note_locks[identity]:
                status = "Конвертирую в текст…" if note["transcript"] is None else "Сохраняю…"
                response = await self.show(owner, status, thread, edit_id=message_id)
                self.store.update_note(owner, identity, status_message_id=response["message_id"])
            return
        if action in {"text", "original"}:
            if note["kind"] == "photo" and not note["transcript"] and not note.get("content"):
                return await self.show(owner, "У фото нет подписи.", thread)
            if note["transcript"] is None:
                raise ValueError("Расшифровка ещё не получена; исходное аудио можно выгрузить отдельно")
            content = note["content"] if action == "text" and note["content"] is not None else note["transcript"]
            for chunk in transcript_chunks(content):
                await self.show(owner, chunk, thread)
            return
        if action == "audio":
            path = self.archive.audio(note)
            if not note["audio_bytes"] or not path.exists():
                raise ValueError("Исходный файл пока недоступен")
            return await self.telegram.document(owner, path, thread,
                                                "Исходный файл без изменений")
        if action == "delete":
            return await self._show(owner,
                "Удалить запись и исходный файл из архива бота?\nИсходное сообщение и расшифровка также будут удалены из чата.", thread,
                keyboard([button("🗑 Да, удалить запись", f"confirmdelete:{identity}"),
                          button("Отмена", f"canceldelete:{identity}")]))
        if action == "canceldelete":
            return await self.remove_menu_message(owner, message_id)
        if action == "confirmarchive":
            if note["state"] == "processing" or self.note_locks[identity].locked():
                raise ValueError("Дождись завершения обработки, затем удали запись")
            self.store.delete(owner, identity)
            self.archive.delete(note)
            await self.remove_menu_message(owner, message_id)
            return await self._show(owner, "Запись удалена из базы. Сообщения в Telegram остались.", thread)
        if action == "confirmdelete":
            if note["state"] == "processing" or self.note_locks[identity].locked():
                raise ValueError("Дождись завершения обработки, затем удали запись")
            self.store.delete(owner, identity)
            self.archive.delete(note)
            result = await self.announce_note_deletion(note, thread)
            await self.remove_menu_message(owner, message_id)
            return result

    async def record_options(self, owner, thread, message):
        note = self.reply_note(owner, message)
        if not note:
            return await self._show(owner,
                "Не нашёл запись. Пришли сообщение в эту папку или ответь на сохранённую запись.", thread)
        self.store.set_action(owner, None)
        identity = note["id"]
        return await self.show_bottom(owner, "Что сделать с этой записью?", thread, keyboard(
            [button("Изменить с ИИ", f"aiask:{identity}"),
             button("Перенести запись", f"recordmove:{identity}")],
            [button("Удалить запись", f"delete:{identity}")]),
            screen=f"recordoptions:{identity}", back="root", keep=True)

    async def delete_reply(self, owner, thread, message):
        note = self.reply_note(owner, message)
        if not note:
            return await self._show(owner, "Не нашёл запись в базе. Ответь на исходное сообщение или расшифровку.", thread)
        identity = note["id"]
        if note["state"] == "processing" or self.note_locks[identity].locked():
            raise ValueError("Дождись завершения обработки, затем удали запись")
        self.store.delete(owner, identity)
        self.archive.delete(note)
        target = (message.get("reply_to_message") or {}).get("message_id")
        return await self.announce_note_deletion(note, thread, target)

    async def announce_note_deletion(self, note, thread, target=None):
        if not target:
            receipt = self.store.one(
                "SELECT message_id FROM note_messages WHERE owner=? AND note_id=? AND deleted=0 "
                "ORDER BY message_id DESC LIMIT 1", (note["owner"], note["id"]))
            target = receipt["message_id"] if receipt else note["message_id"]
        try:
            result = await self.telegram.text(note["owner"], "Сообщение удалено.", thread, reply_to=target)
        except ProviderError as exc:
            if getattr(exc, "reason", None) != "reply_target_missing":
                raise
            result = await self._show(note["owner"], "Сообщение удалено.", thread)
        remaining = await self.delete_note_messages(note)
        if remaining:
            await self._show(note["owner"],
                "Сообщение удалено из базы, но часть сообщений не удалось удалить из Telegram.", thread)
        return result

    async def delete_note_messages(self, note, *, details=False):
        owner, identity = note["owner"], note["id"]
        receipts = self.store.all(
            "SELECT message_id,deleted FROM note_messages WHERE owner=? AND note_id=?",
            (owner, identity))
        removed = {row["message_id"] for row in receipts if row["deleted"]}
        targets = {row["message_id"] for row in receipts if not row["deleted"]}
        # Legacy records retain only these exact IDs. Never infer nearby messages.
        targets.update(note[field] for field in (
            "message_id", "status_message_id", "transcript_message_id", "result_message_id")
            if note.get(field) and note[field] > 0 and note[field] not in removed)
        remaining = retryable = 0
        for target in sorted(targets):
            try:
                await self.telegram.call("deleteMessage", chat_id=owner, message_id=target)
            except ProviderError as exc:
                if getattr(exc, "reason", None) != "delete_target_missing":
                    remaining += 1
                    retryable += getattr(exc, "reason", None) != "delete_forbidden"
                    log.warning("note cleanup failed note_id=%s error=%s", identity, exc)
                    continue
            self.messages.removed(owner, identity, target)
            with self.store.db:
                self.store.db.execute("DELETE FROM telegram_messages WHERE owner=? AND message_id=?",
                                      (owner, target))
        return {"remaining": remaining, "retryable": retryable} if details else remaining

    async def search(self, owner, thread, query):
        if not 1 <= len(query) <= 200:
            raise ValueError("Поисковый запрос должен содержать от 1 до 200 символов")
        matches = self.store.notes(owner, query=query, limit=21)
        rows = [[button("📝 " + n["title"][:60], f"note:{n['id']}")] for n in matches[:20]]
        text = "Ничего не нашёл." if not matches else "Найденные записи:"
        if len(matches) > 20:
            text += "\nПоказаны первые 20. Уточни запрос для остальных."
        rows.append([button("🔎 Другой поиск", "search")])
        await self.show_bottom(owner, text, thread, keyboard(*rows), screen="search_results", back="home",
                               keep=not matches)

    async def settings_menu(self, owner, thread, edit_id=None):
        label = "PDF" if self.store.export_format(owner) == "pdf" else "Markdown"
        enabled = self.store.message_buttons_enabled(owner)
        rows = [[button("Кнопки у сообщений: " + ("вкл" if enabled else "выкл"),
                        f"messagebuttons:{0 if enabled else 1}")],
                [button(f"📦 Экспорт · {label}", "exportsettings:all")]]
        if hasattr(self, "ai_settings"):
            rows.append([button("🤖 Настройки ИИ", "aisettings")])
        rows.append([button("🗑 Удалить всё", "deleteall")])
        text = ("⚙️ Настройки\n\n"
                "Кнопки у сообщений: «Переместить», «Удалить», «ИИ».\n"
                "Когда выключены, новые текстовые сообщения и фото остаются как есть, без замены копией.\n"
                "Архив и расшифровка работают в обоих режимах.")
        await self.show_bottom(owner, text, thread, keyboard(*rows), screen="settings:all",
                               back="home", edit_id=edit_id)

    async def export_settings(self, owner, thread, scope="all", edit_id=None):
        if scope != "all":
            self.store.folder(owner, scope)
        selected = self.store.export_format(owner)
        label = "PDF" if selected == "pdf" else "Markdown"
        await self.show_bottom(owner, f"Формат текста: {label}\n"
                        "Вся история папки — один файл внутри папки проекта.\n"
                        "Выбор сохраняется для следующих выгрузок.", thread, keyboard(
                            [button(("✓ " if selected == "pdf" else "") + "PDF", f"exportformat:pdf:{scope}"),
                             button(("✓ " if selected == "markdown" else "") + "Markdown",
                                    f"exportformat:markdown:{scope}")],
                            [button("📦 К экспорту", f"export:{scope}")]), screen=f"exportsettings:{scope}",
                            back=f"export:{scope}" if scope != "all" else "settings:all", edit_id=edit_id)

    async def export_menu(self, owner, thread, identity, edit_id=None):
        label = "всего архива" if identity == "all" else "папки «" + self.store.folder(owner, identity)["name"] + "»"
        text_format = "PDF" if self.store.export_format(owner) == "pdf" else "Markdown"
        await self.show_bottom(owner, f"📦 Экспорт {label}\n\n"
                        f"Тексты: {text_format}, вся история папки в одном файле. Папки проектов сохранятся.\n"
                        "Всё будет в одном ZIP. Если он больше лимита Telegram — в нескольких частях.",
                        thread, keyboard(
                            [button("Исходники: медиа, тексты и файлы", f"zip:{identity}:raw")],
                            [button(f"Всё: {text_format} + файлы", f"zip:{identity}:all")],
                            [button(f"Только тексты · {text_format}", f"zip:{identity}:text"),
                             button("Только аудио", f"zip:{identity}:voice")],
                            [button("⚙️ Формат текста", f"exportsettings:{identity}")]), screen=f"export:{identity}",
                            back="home" if identity == "all" else f"foldermenu:{identity}", edit_id=edit_id)

    async def send_export(self, owner, thread, identity, content):
        job = self.cleanup.job(owner)
        if owner in self.deleting_owners or job and job["files_pending"]:
            return
        if self.export_locks[owner].locked():
            return await self.show(owner, "Предыдущий экспорт ещё готовится. Дождись его завершения.", thread)
        async with self.export_locks[owner]:
            try:
                if content not in {"all", "text", "voice", "raw"}:
                    raise ValueError("invalid_export_format")
                # Snapshot all metadata and the preference before yielding to other handlers.
                notes = self.store.export_notes(owner, identity)
                folders = [self.store.folder(owner, identity)] if identity else self.store.folders(owner, include_removed=True)
                text_format = self.store.export_format(owner)
                preferences = self.store.export_preferences(owner)
                if content == "voice" and not any(
                        is_audio(n) and (n["file_id"] or n["audio_bytes"]) for n in notes):
                    return await self.show(owner, "В выбранном архиве пока нет аудиозаписей.", thread)
                await self.show(owner, "Готовлю ZIP…", thread)
                export_root = self.config.data_dir / "exports"
                export_root.mkdir(exist_ok=True, mode=0o700)
                with tempfile.TemporaryDirectory(prefix="export-", dir=export_root) as directory:
                    build = asyncio.create_task(asyncio.to_thread(
                        self.archive.export, notes, folders, Path(directory), content != "text",
                        self.config.limits.export_part_bytes, text_format=text_format, audio_only=content == "voice",
                        raw_only=content == "raw", settings=preferences))
                    try:
                        paths = await asyncio.shield(build)
                    except asyncio.CancelledError:
                        # to_thread keeps running on cancellation; do not remove its directory early.
                        await build
                        raise
                    if len(paths) > 1:
                        await self.show(owner, f"Архив большой: отправляю {len(paths)} ZIP-частей.\n"
                                        "Распакуй их в одну папку — файлы распределены между частями.", thread)
                    for i, path in enumerate(paths):
                        label = ("Исходники" if content == "raw" else "Аудио" if content == "voice"
                                 else "PDF" if text_format == "pdf" else "Markdown")
                        if content == "all":
                            label += " + файлы"
                        caption = f"{label} · ZIP" if len(paths) == 1 else f"{label} · часть {i + 1} из {len(paths)}"
                        await self.telegram.document(owner, path, thread, caption)
            except ProviderError as exc:
                log.warning("export delivery error=%s", exc)
                await self.show(owner, "Не удалось подтвердить отправку ZIP. Если файл не пришёл, "
                                "повтори выгрузку. Исходники сохранены.", thread)
            except Exception as exc:
                log.warning("export failed type=%s", type(exc).__name__)
                if isinstance(exc, ValueError) and str(exc) == "archive_audio_pending":
                    message = ("Часть файлов ещё не загружена в архив. Дождись обработки или повтори её "
                               "в карточке записи. Тексты можно выгрузить отдельно.")
                elif isinstance(exc, ValueError) and str(exc) == "export_document_too_large":
                    message = ("История одной темы слишком велика для отправки через Telegram. "
                               "Попробуй другой формат в настройках. Аудио можно выгрузить отдельно.")
                else:
                    message = "Не удалось собрать полный архив. Попробуй ещё раз; записи сохранены."
                await self.show(owner, message, thread)

    async def move(self, owner, identity, folder_id, thread):
        note = self.store.note(owner, identity)
        folder = self.store.folder(owner, folder_id)
        if folder['removed']:
            raise ValueError('Папка удалена. Выбери другую')
        if note["state"] in {"processing", "queued"} or self.note_locks[identity].locked():
            raise ValueError("Дождись завершения обработки, затем перенеси запись")
        async with self.note_locks[identity]:
            if folder_id == note["folder_id"]:
                return "Запись уже в этой папке."
            if note["file_id"] and not note["audio_bytes"]:
                raise ValueError("Сначала повтори сохранение файла, затем перенеси запись")
            move = self.messages.move_archive(note, folder_id)
            result = await self.cleanup_move(move)
            return f"Перенесено в «{folder['name']}»." if result == "Запись перенесена." else result

    async def cleanup_move(self, move):
        owner, identity = move["owner"], move["note_id"]
        remaining = {row["message_id"] for row in self.store.all(
            "SELECT message_id FROM note_messages WHERE owner=? AND note_id=? AND deleted=0",
            (owner, identity))}
        transient, forbidden = False, False
        for message_id in json.loads(move["source_ids"]):
            if message_id not in remaining:
                continue
            try:
                await self.telegram.call("deleteMessage", chat_id=owner, message_id=message_id)
            except ProviderError as exc:
                reason = getattr(exc, "reason", None)
                if reason == "delete_forbidden":
                    forbidden = True
                    continue
                if reason != "delete_target_missing":
                    transient = True
                    continue
            self.messages.removed(owner, identity, message_id)
            with self.store.db:
                self.store.db.execute("DELETE FROM telegram_messages WHERE owner=? AND message_id=?",
                                      (owner, message_id))
        if transient:
            self.messages.state(owner, identity, "cleanup")
            return "Запись перенесена. Удаляю исходные сообщения…"
        self.messages.state(owner, identity, "blocked" if forbidden else "done")
        return ("Запись перенесена, но Telegram не разрешил удалить часть исходных сообщений."
                if forbidden else "Запись перенесена.")

    async def probe_duration(self, path):
        process = await asyncio.create_subprocess_exec("ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "json", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "ogg,mp3,wav,flac,mov,matroska,webm,mpeg,aac",
            "-max_alloc", "67108864", "-probesize", "1048576", "-analyzeduration", "5000000", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 15)
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        try:
            duration = float(json.loads(stdout)["format"]["duration"])
            if process.returncode or not 0 < duration <= self.config.limits.max_audio_seconds:
                raise ValueError
            return max(1, int(duration + .999))
        except (ValueError, TypeError, KeyError):
            raise ProviderError("audio_duration_invalid") from None

    async def process(self, note):
        owner, identity = note["owner"], note["id"]
        async with self.note_locks[identity]:
            note = self.store.note(owner, identity)
            if note["kind"] == "photo":
                return await self.process_photo(note)
            if note["kind"] in {"file", "video"}:
                return await self.process_file(note)
            try:
                if note["file_id"] and note["transcript"] is None:
                    path = self.archive.audio(note)
                    if not path.exists():
                        size, checksum = await self.telegram.download(note["file_id"], path,
                                                                     self.config.limits.max_file_bytes)
                    else:
                        size = path.stat().st_size
                        with path.open("rb") as handle:
                            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
                    self.store.update_note(owner, identity, audio_bytes=size, sha256=checksum)
                    if size > 24_000_000:
                        raise ProviderError("asr_file_too_large")
                    duration = await self.probe_duration(path)
                    self.store.reserve_asr(owner, duration)
                    transcript = await self.groq.transcribe(path)
                    if not transcript:
                        raise ProviderError("no_speech")
                    self.store.update_note(owner, identity, transcript=transcript)
                    note = self.store.note(owner, identity)
                    self.archive.persist_text(note, self.store.folder(owner, note["folder_id"]))
                # Deliver the text before any optional AI address detection.
                await self.deliver_transcript(note)
                note = self.store.note(owner, identity)
                self.capture_ai_input(note, {"message_id": note["message_id"],
                    "message_thread_id": note["source_thread"],
                    "reply_to_message": {"message_id": note["reply_message_id"]}})
                if await self.route_ai(note):
                    return
                # The selected custom folder is fixed on acceptance; note contents never move it.
                self.store.update_note(owner, identity, state="ready", title=fallback_title(note["transcript"]),
                                       routing="manual", requested_folder=None, error=None)
                note = self.store.note(owner, identity)
                self.archive.persist_text(note, self.store.folder(owner, note["folder_id"]))
            except (ProviderError, ValueError, OSError, TimeoutError) as exc:
                error = str(exc) if isinstance(exc, ProviderError) or str(exc) == "daily_asr_limit" else type(exc).__name__
                log.warning("note processing failed note_id=%s error=%s", identity, error)
                self.store.update_note(owner, identity, state="asr_failed", error=error)
                note = self.store.note(owner, identity)
            await self.deliver(note)

    async def process_file(self, note):
        owner, identity = note['owner'], note['id']
        try:
            path = self.archive.audio(note)
            if path.is_file():
                size = path.stat().st_size
                with path.open('rb') as handle:
                    checksum = hashlib.file_digest(handle, 'sha256').hexdigest()
            else:
                size, checksum = await self.telegram.download(note['file_id'], path, self.config.limits.max_file_bytes)
            title = note.get('original_name') or ('Видео' if note['kind'] == 'video' else 'Файл')
            self.store.update_note(owner, identity, audio_bytes=size, sha256=checksum,
                                   state='ready', title=title[:80], routing='manual', error=None)
            note = self.store.note(owner, identity)
            self.archive.persist_text(note, self.store.folder(owner, note['folder_id']))
            text = f"📎 {title}\nФайл сохранён." + (f"\n\n{note['transcript']}" if note['transcript'] else '')
            keys = self.note_controls(note, note['source_thread'])
        except (ProviderError, OSError, ValueError) as exc:
            self.store.update_note(owner, identity, state='asr_failed', error=type(exc).__name__)
            text = 'Не удалось сохранить файл. Оригинал в чате остаётся; попробуй ещё раз.'
            keys = keyboard([button('Повторить', f'retry:{identity}')])
        self.store.update_note(owner, identity, delivery='sending', transcript_delivery='sending')
        edit_id = note['status_message_id']
        self.store.update_note(owner, identity, status_message_id=None)
        try:
            response = await self._show(owner, text, note['source_thread'], keys, edit_id)
            self.messages.record(note, response['message_id'], note['source_thread'], 'file_controls')
            self.store.update_note(owner, identity, delivery='sent', transcript_delivery='sent',
                                   transcript_message_id=response['message_id'])
        except ProviderError:
            self.store.update_note(owner, identity, delivery='unknown', transcript_delivery='unknown')
            raise

    async def photo_controls(self, note, error=False):
        note = self.store.note(note["owner"], note["id"])
        if note["transcript_delivery"] in {"sending", "unknown"}:
            return
        if not error and not self.store.message_buttons_enabled(note["owner"]):
            message_id = note["result_message_id"] or note["message_id"]
            self.store.update_note(note["owner"], note["id"], transcript_delivery="sent",
                                   transcript_message_id=message_id, result_message_id=message_id,
                                   result_thread=note["source_thread"])
            return
        edit_id = note["transcript_message_id"] if note["transcript_delivery"] == "sent" else None
        text = "Не удалось сохранить фото. Попробуй ещё раз." if error else "📷 Фото"
        keys = keyboard([button("Повторить", f"retry:{note['id']}")]) if error else self.note_controls(note, note["source_thread"])
        self.store.update_note(note["owner"], note["id"], transcript_delivery="sending")
        try:
            if error:
                response = await self._show(note["owner"], text, note["source_thread"], keys, edit_id)
                self.messages.record(note, response["message_id"], note["source_thread"], "photo_controls")
            elif note["result_message_id"] and note["result_message_id"] != note["message_id"]:
                await self.telegram.call("editMessageReplyMarkup", chat_id=note["owner"],
                    message_id=note["result_message_id"], reply_markup=keys)
                response = {"message_id": note["result_message_id"]}
            else:
                response = await self.telegram.call("sendPhoto", chat_id=note["owner"],
                    photo=note["file_id"], caption=note["transcript"] or None,
                    message_thread_id=note["source_thread"], reply_markup=keys,
                    reply_parameters={"message_id": note["message_id"]} if note["source_thread"] is None else None)
                note = self.adopt_source_topic(note, response)
                self.messages.record(note, response["message_id"], note["source_thread"], "photo_repost")
                self.store.update_note(note["owner"], note["id"], result_message_id=response["message_id"],
                                       result_thread=note["source_thread"])
            self.store.update_note(note["owner"], note["id"], transcript_delivery="sent",
                                   transcript_message_id=response["message_id"])
        except ProviderError:
            self.store.update_note(note["owner"], note["id"], transcript_delivery="unknown")
            log.warning("photo delivery unknown note_id=%s", note["id"])

    async def cleanup_photo_source(self, note):
        if not self.store.message_buttons_enabled(note["owner"]):
            return
        if note["transcript_delivery"] != "sent" or not self.store.one(
            "SELECT 1 FROM note_messages WHERE owner=? AND note_id=? AND kind='photo_repost' AND deleted=0",
            (note["owner"], note["id"])):
            return
        source = self.store.one("SELECT * FROM note_messages WHERE owner=? AND note_id=? "
                                "AND kind='source' AND deleted=0", (note["owner"], note["id"]))
        if not source:
            return
        try:
            await self.telegram.call("deleteMessage", chat_id=note["owner"], message_id=source["message_id"])
        except ProviderError as exc:
            if getattr(exc, "reason", None) != "delete_target_missing":
                log.warning("photo source cleanup deferred note_id=%s", note["id"])
                return
        self.messages.removed(note["owner"], note["id"], source["message_id"])
        with self.store.db:
            self.store.db.execute("DELETE FROM telegram_messages WHERE owner=? AND message_id=?",
                                  (note["owner"], source["message_id"]))

    async def process_photo(self, note):
        owner, identity = note["owner"], note["id"]
        try:
            source = json.loads(note.get("source_json") or "{}")
            if self.reply_input(owner, source):
                self.capture_ai_input(note, {**source, "message_thread_id": note["source_thread"]})
            path = self.archive.audio(note)
            if path.exists():
                size = path.stat().st_size
                with path.open("rb") as handle:
                    checksum = hashlib.file_digest(handle, "sha256").hexdigest()
            else:
                size, checksum = await self.telegram.download(note["file_id"], path, self.config.limits.max_file_bytes)
            self.store.update_note(owner, identity, audio_bytes=size, sha256=checksum, state="ready",
                                   title=(note["transcript"] or "Фото")[:80], routing="manual", error=None)
            await self.route_ai(self.store.note(owner, identity), detect=False)
            note = self.store.note(owner, identity)
            self.archive.persist_text(note, self.store.folder(owner, note["folder_id"]))
            await self.clear_status(note)
        except (ProviderError, OSError, ValueError) as exc:
            self.store.update_note(owner, identity, state="asr_failed", error=type(exc).__name__)
            await self.clear_status(note)
            try:
                await self.photo_controls(note, error=True)
            except ProviderError:
                pass
            self.store.update_note(owner, identity, delivery="unknown")
            return
        await self.deliver(self.store.note(owner, identity))

    async def clear_status(self, note):
        owner, identity = note["owner"], note["id"]
        message_id = self.store.note(owner, identity)["status_message_id"]
        if message_id is None:
            return
        try:
            await self.telegram.call("deleteMessage", chat_id=owner, message_id=message_id)
        except ProviderError as exc:
            if getattr(exc, "reason", None) != "delete_target_missing":
                log.warning("status cleanup failed note_id=%s error=%s", identity, exc)
                return
        # A full cleanup can finish while this best-effort status deletion is in flight.
        if self.store.one("SELECT 1 FROM notes WHERE owner=? AND id=? AND deleted=0", (owner, identity)):
            self.store.update_note(owner, identity, status_message_id=None)
        self.messages.removed(owner, identity, message_id)

    async def send_transcript(self, note, thread, reply_to=None, source=False, edit_id=None):
        content = note["content"] if note.get("content") is not None else note["transcript"]
        chunks = list(transcript_chunks(content))
        for index, text in enumerate(chunks):
            keys = self.note_controls(note, thread) if index == len(chunks) - 1 else None
            if index == 0 and edit_id is not None:
                response = await self._show(note["owner"], text, thread, keys, edit_id)
            else:
                response = await self.telegram.text(note["owner"], text, thread, keys, reply_to=reply_to)
            if source:
                note = self.adopt_source_topic(note, response)
                thread = note["source_thread"]
                if thread is not None:
                    reply_to = None
            self.messages.record(note, response["message_id"], thread, "transcript")
        return response, thread

    async def deliver_transcript(self, note):
        owner, identity = note["owner"], note["id"]
        note = self.store.note(owner, identity)
        if not note["transcript"] or note["transcript_delivery"] != "pending":
            return
        if note["kind"] == "text" and not self.store.message_buttons_enabled(owner):
            # The original is the visible text and the reply target; persist it so
            # retries, AI replies and later moves use the same exact Telegram ID.
            self.store.update_note(owner, identity, transcript_delivery="sent",
                                   transcript_message_id=note["message_id"])
            return
        edit_id = note["status_message_id"]
        # The status becomes archive text: never let later cleanup delete it, even
        # if Telegram accepts the edit but its response is lost or the process exits.
        self.store.update_note(owner, identity, transcript_delivery="sending", status_message_id=None)
        try:
            response, _ = await self.send_transcript(note, note["source_thread"], source=True, edit_id=edit_id)
            self.store.update_note(owner, identity, transcript_delivery="sent",
                                   transcript_message_id=response["message_id"])
        except ProviderError as exc:
            self.store.update_note(owner, identity, transcript_delivery="unknown")
            log.warning("transcript delivery unknown note_id=%s error=%s", identity, exc)
            return
        if note["kind"] == "text" and self.store.message_buttons_enabled(owner):
            try:
                await self.telegram.call("deleteMessage", chat_id=owner, message_id=note["message_id"])
            except ProviderError as exc:
                if getattr(exc, "reason", None) != "delete_target_missing":
                    log.warning("text source cleanup deferred note_id=%s error=%s", identity, exc)
                    return
            self.messages.removed(owner, identity, note["message_id"])
            with self.store.db:
                self.store.db.execute("DELETE FROM telegram_messages WHERE owner=? AND message_id=?",
                                      (owner, note["message_id"]))

    async def deliver(self, note):
        owner, identity = note["owner"], note["id"]
        if self.store.note(owner, identity)["delivery"] != "pending":
            return
        if note["kind"] == "photo":
            if note["state"] != "ready":
                return
            await self.photo_controls(note)
            saved = self.store.note(owner, identity)
            self.store.update_note(owner, identity, delivery=saved["transcript_delivery"])
            await self.cleanup_photo_source(saved)
            return
        # Mark intent before sending so an uncertain delivery is never replayed.
        self.store.update_note(owner, identity, delivery="sending")
        try:
            if not note["transcript"] and note["kind"] != "photo":
                await self.clear_status(note)
                text = "Не удалось конвертировать запись в текст. Попробуй ещё раз."
                keys = keyboard([button("Повторить", f"retry:{identity}")])
                response = await self.telegram.text(owner, text, note["source_thread"], keys)
                note = self.adopt_source_topic(note, response)
                self.store.update_note(owner, identity, delivery="sent", result_message_id=response["message_id"],
                                       result_thread=note["source_thread"])
                return
            if note["kind"] != "photo":
                await self.deliver_transcript(note)
            await self.clear_status(note)
            saved = self.store.note(owner, identity)
            self.store.update_note(owner, identity, delivery=saved["transcript_delivery"],
                                   result_message_id=saved["transcript_message_id"],
                                   result_thread=saved["source_thread"])
            # Explicit moves separately clean up the saved Telegram representations.
        except ProviderError as exc:
            self.store.update_note(owner, identity, delivery="unknown")
            log.warning("note delivery unknown note_id=%s error=%s method=%s reason=%s", identity, exc,
                        getattr(exc, "method", "unknown"), getattr(exc, "reason", "unknown"))

    async def worker(self):
        while True:
            if self.view_rendering:
                await asyncio.sleep(.2)
                continue
            note = self.store.claim_note()
            if note is None:
                # Processing may have finished just before a crash and before delivery began.
                note = self.store.one("SELECT * FROM notes WHERE state IN ('ready','asr_failed') "
                                      "AND delivery='pending' AND deleted=0 ORDER BY created_at LIMIT 1")
                if note:
                    await self.deliver(note)
                    self.view_refresh_event.set()
                else:
                    photo = self.store.one("SELECT n.* FROM notes n JOIN note_messages m ON m.note_id=n.id "
                        "WHERE n.kind='photo' AND n.deleted=0 AND n.transcript_delivery='sent' "
                        "AND m.kind='source' AND m.deleted=0 "
                        "AND EXISTS (SELECT 1 FROM user_message_settings s WHERE s.owner=n.owner AND s.buttons_enabled=1) "
                        "AND EXISTS (SELECT 1 FROM note_messages r "
                        "WHERE r.note_id=n.id AND r.kind='photo_repost' AND r.deleted=0) LIMIT 1")
                    if photo:
                        await self.cleanup_photo_source(photo)
                    move = self.messages.pending()
                    if move and not self.note_locks[move["note_id"]].locked():
                        async with self.note_locks[move["note_id"]]:
                            await self.cleanup_move(move)
                    stale = self.store.one("SELECT * FROM notes WHERE status_message_id IS NOT NULL "
                                           "AND state IN ('ready','asr_failed') AND deleted=0 "
                                           "ORDER BY created_at LIMIT 1")
                    if stale:
                        await self.clear_status(stale)
                    if stale or move or photo:
                        self.view_refresh_event.set()
                    await asyncio.sleep(3 if stale or move or photo else .5)
                continue
            await self.process(note)
            self.view_refresh_event.set()
