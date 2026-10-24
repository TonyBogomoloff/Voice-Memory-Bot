from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path

from .ai_bot import AIBotMixin
from .archive import Archive
from .cleanup import CleanupMixin
from .config import Config
from .folder_menu import MENU_COMMANDS, FolderMenuMixin
from .message_store import MessageStore
from .providers import CodexRouter, Groq, ProviderError, Telegram
from .routing import fallback_title
from .store import Store, clean_name

log = logging.getLogger(__name__)


def button(text, data):
    return {"text": text, "callback_data": data}


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

Выбери папку на нижней клавиатуре или через «📂 Все папки».
Новые голосовые, тексты и фото сохраняются туда. Активная папка отмечена ✓.
Внизу — до 4 папок на странице. Стрелки листают страницы; закреплённые папки идут первыми.
«☰ Меню» открывает записи, создание папки, поиск, ИИ, экспорт и настройки.
Выбор и закрепления запоминаются. Без выбора записи попадают во «Входящие».
Внутри папки можно изменить название, посмотреть записи, скачать архив или удалить папку.
При удалении папки записи сохраняются во «Входящих»; сначала бот попросит подтверждение.
Расшифровка появится рядом с голосовым. Для переноса: «Опции» → «Переместить».
Папки общие с Mini App; темы Telegram больше не используются для выбора папки.
/start — показать папки внизу
/menu — открыть меню действий
/app — открыть Mini App

/folders — записи и управление папками
/new Название — создать папку
/notes — последние записи
/search текст — поиск по всему архиву
/export — выгрузить архив
/delete_all — удалить все папки и сообщения с подтверждением
/ai — открыть папку для диалога с ИИ
/settings — настройки ИИ и экспорта PDF / Markdown
/newchat — начать новый разговор
/cancel — отменить ввод названия или поиск

В карточке записи доступны текст, исходное аудио, перенос и удаление.
Голосовые и видеокружки сразу расшифровываются в текст. Под расшифровкой: «Опции» → «Переместить» / «Обратиться к ИИ».
В папке с режимом «Диалог с ИИ» все сообщения идут помощнику. Ответ на реплику ИИ продолжает разговор.
Обращение по имени в начале или конце можно включить в настройках. По умолчанию оно выключено.
ИИ умеет искать, создавать и менять заметки; исходная расшифровка сохраняется, правку можно отменить.
История разговора и запрошенные заметки передаются модели. Доступ к заметкам можно выключить в настройках.
Фото тоже сохраняются: под снимком появится отдельное сообщение с кнопкой «Опции».
Аудио отправляется в Groq для распознавания. OpenAI используется для функций ИИ.
Оригинал и исходная расшифровка хранятся в твоём архиве. Удаление отдельной записи из архива
не удаляет историю Telegram. «Удалить всё» очищает также темы и известные боту сообщения;
старые сообщения вне тем могут потребовать ручной очистки в Telegram.
Автосинхронизация с другими сервисами пока не подключена."""


class Bot(FolderMenuMixin, CleanupMixin, AIBotMixin):
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
        self.web = None

    async def start(self):
        me = await self.telegram.call("getMe")
        webhook = await self.telegram.call("getWebhookInfo")
        if webhook.get("url"):
            raise ProviderError("webhook_present_stop_before_polling")
        await self.telegram.call("setMyCommands", commands=[
            {"command": "start", "description": "Показать папки внизу"},
            {"command": "menu", "description": "Меню действий"},
            *([{"command": "app", "description": "Открыть Mini App"}] if self.config.web_url else []),
            {"command": "folders", "description": "Записи и папки"},
            {"command": "new", "description": "Создать папку"},
            {"command": "notes", "description": "Последние записи"},
            {"command": "search", "description": "Поиск по записям"},
            {"command": "export", "description": "Выгрузить архив"},
            {"command": "delete_all", "description": "Удалить всё"},
            {"command": "ai", "description": "Поговорить с ИИ"},
            {"command": "settings", "description": "Настройки"},
            {"command": "newchat", "description": "Новый разговор с ИИ"},
            {"command": "help", "description": "Как пользоваться"},
            {"command": "cancel", "description": "Отменить ввод"},
        ])
        # Finish physical deletion interrupted by a previous shutdown.
        for note in self.store.all("SELECT * FROM notes WHERE deleted=1"):
            self.archive.delete(note)
        await self.resume_deletions()
        await self.configure_menu()
        if self.config.web_url:
            from .miniapp import MiniApp
            self.web = MiniApp(self)
            await self.web.start()
        log.info("started bot_id=%s folder_mode=custom telegram_topics=%s", me["id"], me.get("has_topics_enabled", False))
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self.poll())
                group.create_task(self.dispatch())
                group.create_task(self.worker())
                group.create_task(self.ai_worker())
        finally:
            if self.web:
                await self.web.close()
            for task in self.export_tasks:
                task.cancel()
            await asyncio.gather(*self.export_tasks, return_exceptions=True)

    async def poll(self):
        while True:
            try:
                if self.store.one("SELECT count(*) AS n FROM updates")["n"] >= 1000:
                    await asyncio.sleep(2)
                    continue
                updates = await self.telegram.call("getUpdates", offset=self.store.offset(), timeout=30,
                                                   allowed_updates=["message", "callback_query"], limit=50)
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
            update = self.store.pending_update()
            if update is None:
                await asyncio.sleep(.2)
                continue
            completed = None
            try:
                await self.handle(update)
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
                if completed is True:
                    self.store.finish_update(update["update_id"])
                elif completed is False:
                    self.store.fail_update(update["update_id"])

    async def show(self, owner, text, thread=None, keys=None, edit_id=None):
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
        message = update.get("message") or (callback or {}).get("message", {})
        sender = callback.get("from", {}) if callback else message.get("from", {})
        owner = sender.get("id")
        if owner not in self.config.allowed_user_ids or message.get("chat", {}).get("type") != "private":
            return
        if message["chat"]["id"] != owner:
            return
        thread = message.get("message_thread_id")
        self.cleanup.remember(owner, message.get("message_id"), thread)
        move_callback = callback and callback.get("data", "").startswith(("place:", "move:", "newfor:"))
        if callback and not move_callback:
            await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"])
        navigation = message.get("text", "").strip()
        if (callback or navigation.startswith("/") or navigation in MENU_COMMANDS
                or self.is_folder_button(navigation)) and not self.rate_allowed(owner):
            if move_callback:
                await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"])
            return
        job = self.cleanup.job(owner)
        if owner in self.deleting_owners or job and job["files_pending"]:
            if owner not in self.deleting_owners and (
                callback and callback.get("data") == "retrydeleteall"
                or message.get("text", "").split("@", 1)[0] == "/delete_all"
            ):
                return await self.resume_delete_all(owner)
            return await self.show(owner, "Очистка ещё не завершена. Повтори её перед отправкой новых заметок.",
                                   keys=keyboard([button("Повторить очистку", "retrydeleteall")]))
        if not callback and thread and self.store.one(
            "SELECT 1 FROM deletion_targets WHERE owner=? AND kind='topic' AND identity=?", (owner, thread)
        ):
            return await self.show(owner, "Эта тема ожидает удаления. Отправь новые заметки в общий чат.",
                                   keys=keyboard([button("Повторить очистку Telegram", "retrydeleteall")]))
        self.store.ensure_user(owner)
        try:
            if callback:
                result = await self.callback(owner, thread, message["message_id"], callback.get("data", ""))
                if move_callback:
                    await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"], text=result)
                return result
            if "forum_topic_created" in message or "forum_topic_edited" in message:
                return
            text = message.get("text", "").strip()
            if text in {"⬅️", "➡️"}:
                return await self.turn_folder_page(owner, thread, text)
            if text in MENU_COMMANDS:
                return await self.command(owner, thread, MENU_COMMANDS[text], "")
            if self.is_folder_button(text):
                return await self.select_folder_button(owner, thread, text)
            if text.startswith("/"):
                command, _, argument = text.partition(" ")
                return await self.command(owner, thread, command.split("@")[0].lower(), argument.strip())
            if self.store.action(owner):
                if text:
                    return await self.input_action(owner, thread, text)
                return await self.show(owner, "Сначала пришли ответ текстом или нажми «Отмена».", thread,
                                       keyboard([button("Отмена", "home")]))
            folder = self.source_folder(owner, thread, message)["id"]
            if not message.get("photo"):
                self.check_ai_capacity(owner, folder, message)
            if text:
                note, created = self.store.add_note(owner, folder, message, "text", transcript=text)
            else:
                kind = next((k for k in ("voice", "video_note", "audio", "document", "photo") if message.get(k)), None)
                if kind is None:
                    return await self.show(owner, "Пришли голосовое, кружок, аудиофайл, фото или текстовую заметку.", thread, HOME)
                media = message[kind][-1] if kind == "photo" else message[kind]
                extension = ("jpg" if kind == "photo" else "mp4" if kind == "video_note" else "ogg" if kind == "voice"
                             else Path(media.get("file_name", "")).suffix.lower().lstrip("."))
                if kind != "photo" and extension not in {"ogg", "mp3", "mp4", "mpeg", "mpga", "m4a", "flac", "wav", "webm"}:
                    raise ValueError("Поддерживаются OGG, MP3, MP4, M4A, FLAC, WAV и WebM")
                size, duration = media.get("file_size", 0), media.get("duration", 0)
                if size <= 0 or size > self.config.limits.max_file_bytes:
                    raise ValueError("Для MVP нужен файл размером до 20 MB")
                if duration > self.config.limits.max_audio_seconds:
                    raise ValueError(f"Максимальная длительность — {self.config.limits.max_audio_seconds // 60} минут")
                # Uploaded documents lack reliable duration; probe the saved file before calling Groq.
                note, created = self.store.add_note(owner, folder, message, kind, media["file_id"],
                                                    extension, size, duration,
                                                    transcript=message.get("caption", "") if kind == "photo" else None)
            if created:
                self.messages.record(note, message["message_id"], thread, "source")
                # The worker uses the same lock, so a quick ASR cannot outrun the status checkpoint.
                async with self.note_locks[note["id"]]:
                    if note["kind"] == "photo":
                        return await self.photo_controls(note)
                    status = "Конвертирую в текст…" if note["file_id"] else "Сохраняю…"
                    response = await self.telegram.text(owner, status, thread,
                        reply_to=note["message_id"] if thread is None else None)
                    self.store.update_note(owner, note["id"], status_message_id=response["message_id"])
                    note = self.adopt_source_topic(note, response)
                    self.capture_ai_input(note, {**message, "message_thread_id": note["source_thread"]})
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
        if command == "/app" and self.config.web_url:
            return await self.show(owner, "Твои папки, голосовые, фото, видео и файлы.", thread,
                {"inline_keyboard": [[{"text": "📂 Открыть мини-эп", "web_app": {"url": self.config.web_url}}]]})
        if command == "/delete_all":
            return await self.delete_all_menu(owner, thread)
        if command == "/ai":
            return await self.open_ai(owner, thread)
        if command == "/newchat":
            return await self.new_ai_conversation(owner, thread, self.store.current_folder(owner, thread))
        if command == "/start":
            return await self.show_home(owner, thread)
        if command == "/menu":
            return await self.show_main_menu(owner, thread)
        if command in {"/help", "/privacy"}:
            return await self.show(owner, HELP, thread, HOME)
        if command == "/cancel":
            self.delete_confirmations.pop(owner, None)
            return await self.show_home(owner, thread)
        if command == "/folders":
            return await self.show_folders(owner, thread)
        if command == "/new":
            if argument:
                return await self.create_folder(owner, thread, argument)
            self.store.set_action(owner, "create")
            return await self.show(owner, "Как назовём папку? Пришли название текстом.", thread,
                                   keyboard([button("Отмена", "home")]))
        if command == "/notes":
            return await self.show_notes(owner, thread, self.store.current_folder(owner, thread))
        if command == "/search":
            if argument:
                return await self.search(owner, thread, argument)
            self.store.set_action(owner, "search")
            return await self.show(owner, "Что найти в твоих заметках? Пришли слова текстом.", thread,
                                   keyboard([button("Отмена", "home")]))
        if command == "/export":
            return await self.export_menu(owner, thread, "all")
        if command == "/settings":
            return await self.settings_menu(owner, thread)
        return await self.show(owner, "Команда не найдена. Открой меню папок или отправь заметку.", thread, HOME)

    async def input_action(self, owner, thread, text):
        action = self.store.action(owner)
        if action == "ai_names":
            self.ai_state.set_settings(owner, names=[n.strip() for n in text.split(",") if n.strip()])
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
        await self.show_notes(owner, thread, folder["id"])

    async def show_folders(self, owner, thread=None, edit_id=None, page=0, note_id=None):
        self.store.set_action(owner, None)
        folders = self.store.folders(owner)
        active = self.store.current_folder(owner)
        await self.refresh_folder_keyboard(owner, thread)
        rows = []
        page = max(0, min(page, (len(folders) - 1) // 8))
        for folder in folders[page * 8:(page + 1) * 8]:
            action = f"move:{note_id}:{folder['id']}" if note_id else f"folder:{folder['id']}:0"
            marker = "✓" if folder["id"] == active else "📁"
            rows.append([button(f"{marker} {folder['name']} · {folder['count']}", action)])
        nav = []
        mode = f"choose:{note_id}" if note_id else "folders"
        if page:
            nav.append(button("←", f"{mode}:{page - 1}"))
        if len(folders) > (page + 1) * 8:
            nav.append(button("→", f"{mode}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([button("＋ Новая папка", "create"), button("🔎 Поиск", "search")])
        rows.append([button("⌂ Меню", "home")])
        name = self.store.folder(owner, active)["name"]
        text = "Куда переместить запись?" if note_id else (
            f"📁 Твои папки\nСейчас сохраняю в «{name}».\nОткрой папку, чтобы выбрать её для новых записей.")
        await self.show(owner, text, thread, keyboard(*rows), edit_id)

    async def show_notes(self, owner, thread, identity, page=0, edit_id=None):
        folder = self.store.folder(owner, identity)
        self.store.set_action(owner, None)
        self.store.select(owner, identity)
        await self.refresh_folder_keyboard(owner, thread)
        page = max(0, page)
        notes = self.store.notes(owner, identity, offset=page * 8, limit=9)
        text = f"📁 {folder['name']}"
        text += "\nРежим: " + ("диалог с ИИ" if folder["mode"] == "chat" else "заметки")
        text += "\n✓ Новые записи сохраняются сюда. Пришли голосовое, текст или фото."
        text += "\nЗаписи:" if notes else "\nПока нет записей."
        rows = [[button(n["title"][:55], f"note:{n['id']}")] for n in notes[:8]]
        nav = []
        if page:
            nav.append(button("←", f"folder:{identity}:{page - 1}"))
        if len(notes) > 8:
            nav.append(button("→", f"folder:{identity}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([button("📦 Экспорт", f"export:{identity}")])
        if not folder["is_inbox"]:
            rows[-1].append(button("✏️ Название", f"rename:{identity}"))
            rows.append([button("🗑 Удалить папку", f"dropfolder:{identity}")])
        pin_action = "unpin" if folder["pin_slot"] is not None else "pin"
        rows.append([button("📌 Убрать снизу" if pin_action == "unpin" else "📌 Закрепить внизу",
                            f"{pin_action}:{identity}")])
        target_mode = "notes" if folder["mode"] == "chat" else "chat"
        rows.append([button("Режим → " + ("Заметки" if target_mode == "notes" else "Диалог с ИИ"),
                            f"aimode:{identity}:{target_mode}")])
        if folder["mode"] == "chat":
            rows.append([button("Новый разговор", f"ainew:{identity}"),
                         button("История ИИ", f"ailist:{identity}:0")])
        rows.append([button("← Папки", "folders"), button("⌂ Меню", "home")])
        await self.show(owner, text, thread, keyboard(*rows), edit_id)

    def note_card(self, note):
        folder = self.store.folder(note["owner"], note["folder_id"])
        date = datetime.fromtimestamp(note["created_at"], UTC).strftime("%d.%m.%Y %H:%M UTC")
        text = f"📁 {folder['name']}\n{note['title']}\n{date}"
        if note["state"] in {"queued", "processing"}:
            text += "\n⏳ Обрабатываю запись."
        elif note["state"] == "asr_failed":
            if note["kind"] == "photo":
                text += "\nНе удалось сохранить фото. Попробуй ещё раз."
            else:
                text += "\nНе удалось получить расшифровку."
                text += " Аудио сохранено." if note["audio_bytes"] else " Загрузка аудио пока не завершена."
            if note["error"] == "daily_asr_limit":
                text += " Достигнут лимит распознавания за последние 24 часа."
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
            rows.append([button("📷 Фото" if note["kind"] == "photo" else "📹 Кружок" if note["kind"] == "video_note" else "🎙 Аудио", f"audio:{identity}")])
        if note.get("content") is not None:
            rows.append([button("Исходная расшифровка", f"original:{identity}"),
                         button("Отменить правку", f"aiundo:{identity}:{note['revision']}")])
        if note["kind"] != "photo":
            rows.append([button("Обратиться к ИИ", f"aiask:{identity}")])
        if note["state"] == "asr_failed" or note["routing"] in {"unavailable", "review"}:
            rows.append([button("Повторить обработку", f"retry:{identity}")])
        if note["requested_folder"] and note["routing"] == "review":
            rows.append([button(f"＋ Создать «{note['requested_folder'][:35]}»", f"newfor:{identity}")])
        rows.append([button("Удалить из архива", f"delete:{identity}"), button("Папки", "folders")])
        return text[:1000], keyboard(*rows)

    async def show_note(self, owner, identity, thread, edit_id=None):
        note = self.store.note(owner, identity)
        text, keys = self.note_card(note)
        await self.show(owner, text, thread, keys, edit_id)

    async def edit_note_buttons(self, owner, message_id, keys):
        try:
            await self.telegram.call("editMessageReplyMarkup", chat_id=owner, message_id=message_id,
                                     reply_markup=keys)
        except ProviderError as exc:
            if getattr(exc, "reason", None) != "not_modified":
                raise

    async def choose_note_folder(self, owner, identity, message_id, page):
        folders = self.store.folders(owner)
        page = max(0, min(page, (len(folders) - 1) // 8))
        rows = [[button(f"📁 {folder['name']}", f"place:{identity}:{folder['id']}")]
                for folder in folders[page * 8:(page + 1) * 8]]
        nav = []
        if page:
            nav.append(button("←", f"pick:{identity}:{page - 1}"))
        if len(folders) > (page + 1) * 8:
            nav.append(button("→", f"pick:{identity}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([button("← Назад", f"options:{identity}")])
        await self.edit_note_buttons(owner, message_id, keyboard(*rows))

    async def callback(self, owner, thread, message_id, data):
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
            return await self.show(owner, HELP, thread, HOME, message_id)
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
            prompt = "Пришли новое название." if action == "rename" else "Пришли названия через запятую. «-» удалит псевдонимы."
            return await self.show(owner, prompt, thread, keyboard([button("Отмена", f"folder:{identity}:0")]))
        if action == "export":
            return await self.export_menu(owner, thread, identity, message_id)
        if action == "settings":
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
            if len(pieces) != 3 or pieces[2] not in {"all", "text", "voice", "audio"}:
                raise ValueError("Выбери вид экспорта в меню /export")
            folder_id = None if identity == "all" else identity
            if folder_id:
                self.store.folder(owner, folder_id)
            # Keep old 'texts + audio' buttons working after the update.
            content = "all" if pieces[2] == "audio" else pieces[2]
            task = asyncio.create_task(self.send_export(owner, thread, folder_id, content))
            self.export_tasks.add(task)
            task.add_done_callback(self.export_tasks.discard)
            return
        note = self.store.note(owner, identity)
        if action == "options":
            rows = [[button("Переместить", f"pick:{identity}:0")]]
            if note["kind"] != "photo":
                rows.append([button("Обратиться к ИИ", f"aiask:{identity}")])
            rows.append([button("← Назад", f"collapse:{identity}")])
            return await self.edit_note_buttons(owner, message_id, keyboard(*rows))
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
                                                "Фото" if note["kind"] == "photo" else "Исходный видеокружок" if note["kind"] == "video_note" else "Исходное аудио без перекодирования")
        if action == "delete":
            return await self.show(owner, "Удалить запись и исходный файл из архива бота?\nСообщения в Telegram останутся.",
                thread, keyboard([button("Удалить", f"confirmdelete:{identity}"), button("Отмена", f"note:{identity}")]))
        if action == "confirmdelete":
            if note["state"] == "processing" or self.note_locks[identity].locked():
                raise ValueError("Дождись завершения обработки, затем удали запись")
            self.store.delete(owner, identity)
            self.archive.delete(note)
            return await self.show(owner, "Запись удалена из архива.", thread, HOME, message_id)

    async def search(self, owner, thread, query):
        if not 1 <= len(query) <= 200:
            raise ValueError("Поисковый запрос должен содержать от 1 до 200 символов")
        matches = self.store.notes(owner, query=query, limit=21)
        rows = [[button(n["title"][:60], f"note:{n['id']}")] for n in matches[:20]]
        text = "Ничего не нашёл." if not matches else "Найденные записи:"
        if len(matches) > 20:
            text += "\nПоказаны первые 20. Уточни запрос для остальных."
        rows.append([button("Папки", "folders")])
        await self.show(owner, text, thread, keyboard(*rows))

    async def settings_menu(self, owner, thread, edit_id=None):
        label = "PDF" if self.store.export_format(owner) == "pdf" else "Markdown"
        rows = [[button(f"📦 Экспорт · {label}", "exportsettings:all")]]
        if hasattr(self, "ai_settings"):
            rows.append([button("🤖 Настройки ИИ", "aisettings")])
        rows.append([button("🗑 Удалить всё", "deleteall")])
        rows.append([button("📁 Папки", "folders"), button("⌂ Меню", "home")])
        await self.show(owner, "⚙️ Настройки", thread, keyboard(*rows), edit_id)

    async def export_settings(self, owner, thread, scope="all", edit_id=None):
        if scope != "all":
            self.store.folder(owner, scope)
        selected = self.store.export_format(owner)
        label = "PDF" if selected == "pdf" else "Markdown"
        await self.show(owner, f"⚙️ Настройки\n\nФормат экспорта текста: {label}.\n"
                        "Вся история папки — один файл внутри папки проекта.\n"
                        "Выбор сохраняется для следующих выгрузок.", thread, keyboard(
                            [button(("✓ " if selected == "pdf" else "") + "PDF", f"exportformat:pdf:{scope}"),
                             button(("✓ " if selected == "markdown" else "") + "Markdown",
                                    f"exportformat:markdown:{scope}")],
                            [button("📦 К экспорту", f"export:{scope}"), button("← Настройки", "settings:all")]), edit_id)

    async def export_menu(self, owner, thread, identity, edit_id=None):
        label = "всего архива" if identity == "all" else "папки «" + self.store.folder(owner, identity)["name"] + "»"
        text_format = "PDF" if self.store.export_format(owner) == "pdf" else "Markdown"
        await self.show(owner, f"📦 Экспорт {label}\n\n"
                        f"Тексты: {text_format}, вся история папки в одном файле. Папки проектов сохранятся.\n"
                        "Всё будет в одном ZIP. Если он больше лимита Telegram — в нескольких частях.",
                        thread, keyboard(
                            [button(f"Всё: {text_format} + файлы", f"zip:{identity}:all")],
                            [button(f"Только тексты · {text_format}", f"zip:{identity}:text"),
                             button("Только аудио", f"zip:{identity}:voice")],
                            [button("⚙️ Формат текста", f"exportsettings:{identity}"), button("← Папки", "folders")]), edit_id)

    async def send_export(self, owner, thread, identity, content):
        job = self.cleanup.job(owner)
        if owner in self.deleting_owners or job and job["files_pending"]:
            return
        if self.export_locks[owner].locked():
            return await self.show(owner, "Предыдущий экспорт ещё готовится. Дождись его завершения.", thread)
        async with self.export_locks[owner]:
            try:
                if content not in {"all", "text", "voice"}:
                    raise ValueError("invalid_export_format")
                # Snapshot all metadata and the preference before yielding to other handlers.
                notes = self.store.export_notes(owner, identity)
                folders = [self.store.folder(owner, identity)] if identity else self.store.folders(owner)
                text_format = self.store.export_format(owner)
                preferences = self.store.export_preferences(owner)
                if content == "voice" and not any(
                        n["kind"] != "photo" and (n["file_id"] or n["audio_bytes"]) for n in notes):
                    return await self.show(owner, "В выбранном архиве пока нет аудиозаписей.", thread)
                await self.show(owner, "Готовлю ZIP…", thread)
                export_root = self.config.data_dir / "exports"
                export_root.mkdir(exist_ok=True, mode=0o700)
                with tempfile.TemporaryDirectory(prefix="export-", dir=export_root) as directory:
                    build = asyncio.create_task(asyncio.to_thread(
                        self.archive.export, notes, folders, Path(directory), content != "text",
                        self.config.limits.export_part_bytes, text_format=text_format, audio_only=content == "voice",
                        settings=preferences))
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
                        label = "Аудио" if content == "voice" else "PDF" if text_format == "pdf" else "Markdown"
                        if content == "all":
                            label += " + файлы"
                        caption = f"{label} · ZIP" if len(paths) == 1 else f"{label} · часть {i + 1} из {len(paths)}"
                        await self.telegram.document(owner, path, thread, caption)
            except ProviderError as exc:
                log.warning("export delivery error=%s", exc)
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
        if note["state"] in {"processing", "queued"} or self.note_locks[identity].locked():
            raise ValueError("Дождись завершения обработки, затем перенеси запись")
        async with self.note_locks[identity]:
            if folder_id == note["folder_id"]:
                return "Запись уже в этой папке."
            self.store.update_note(owner, identity, folder_id=folder_id, routing="manual",
                                   requested_folder=None, archive_dirty=1)
            return f"Перенесено в «{folder['name']}»."

    async def cleanup_move(self, move):
        owner, identity = move["owner"], move["note_id"]
        remaining = set(self.messages.in_thread(owner, identity, move["source_thread"]))
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
            if note.get("origin") == "web":
                from .miniapp import process_web_note
                return await process_web_note(self, note)
            if note["kind"] == "photo":
                return await self.process_photo(note)
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

    async def photo_controls(self, note, error=False):
        note = self.store.note(note["owner"], note["id"])
        if note["transcript_delivery"] in {"sending", "unknown"}:
            return
        edit_id = note["transcript_message_id"] if note["transcript_delivery"] == "sent" else None
        text = "Не удалось сохранить фото. Попробуй ещё раз." if error else "📷 Фото"
        keys = keyboard([button("Повторить", f"retry:{note['id']}")]) if error else self.note_controls(note, note["source_thread"])
        self.store.update_note(note["owner"], note["id"], transcript_delivery="sending")
        try:
            if edit_id:
                response = await self.show(note["owner"], text, note["source_thread"], keys, edit_id)
            else:
                response = await self.telegram.text(note["owner"], text, note["source_thread"], keys,
                    reply_to=note["message_id"] if note["source_thread"] is None else None)
                note = self.adopt_source_topic(note, response)
            self.messages.record(note, response["message_id"], note["source_thread"], "photo_controls")
            self.store.update_note(note["owner"], note["id"], transcript_delivery="sent",
                                   transcript_message_id=response["message_id"])
        except ProviderError:
            self.store.update_note(note["owner"], note["id"], transcript_delivery="unknown")
            raise

    async def process_photo(self, note):
        owner, identity = note["owner"], note["id"]
        try:
            path = self.archive.audio(note)
            if path.exists():
                size = path.stat().st_size
                with path.open("rb") as handle:
                    checksum = hashlib.file_digest(handle, "sha256").hexdigest()
            else:
                size, checksum = await self.telegram.download(note["file_id"], path, self.config.limits.max_file_bytes)
            self.store.update_note(owner, identity, audio_bytes=size, sha256=checksum, state="ready",
                                   title=(note["transcript"] or "Фото")[:80], routing="manual", error=None)
            note = self.store.note(owner, identity)
            self.archive.persist_text(note, self.store.folder(owner, note["folder_id"]))
            await self.clear_status(note)
            if note["transcript_delivery"] == "pending":
                await self.photo_controls(note)
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

    async def send_transcript(self, note, thread, reply_to=None, source=False):
        content = note["content"] if note.get("content") is not None else note["transcript"]
        chunks = list(transcript_chunks(content))
        for index, text in enumerate(chunks):
            keys = self.note_controls(note, thread) if index == len(chunks) - 1 else None
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
        await self.clear_status(note)
        self.store.update_note(owner, identity, transcript_delivery="sending")
        try:
            response, _ = await self.send_transcript(note, note["source_thread"], source=True,
                reply_to=note["message_id"] if note["source_thread"] is None else None)
            self.store.update_note(owner, identity, transcript_delivery="sent",
                                   transcript_message_id=response["message_id"])
        except ProviderError as exc:
            self.store.update_note(owner, identity, transcript_delivery="unknown")
            log.warning("transcript delivery unknown note_id=%s error=%s", identity, exc)

    async def deliver(self, note):
        owner, identity = note["owner"], note["id"]
        if self.store.note(owner, identity)["delivery"] != "pending":
            return
        # Mark intent before sending so an uncertain delivery is never replayed.
        self.store.update_note(owner, identity, delivery="sending")
        try:
            await self.clear_status(note)
            if not note["transcript"] and note["kind"] != "photo":
                text = "Не удалось конвертировать запись в текст. Попробуй ещё раз."
                keys = keyboard([button("Повторить", f"retry:{identity}")])
                response = await self.telegram.text(owner, text, note["source_thread"], keys,
                    reply_to=note["message_id"] if note["source_thread"] is None else None)
                note = self.adopt_source_topic(note, response)
                self.store.update_note(owner, identity, delivery="sent", result_message_id=response["message_id"],
                                       result_thread=note["source_thread"])
                return
            if note["kind"] != "photo":
                await self.deliver_transcript(note)
            saved = self.store.note(owner, identity)
            self.store.update_note(owner, identity, delivery=saved["transcript_delivery"],
                                   result_message_id=saved["transcript_message_id"],
                                   result_thread=saved["source_thread"])
            # Moving a note only changes its archive folder; Telegram history stays in place.
        except ProviderError as exc:
            self.store.update_note(owner, identity, delivery="unknown")
            log.warning("note delivery unknown note_id=%s error=%s method=%s reason=%s", identity, exc,
                        getattr(exc, "method", "unknown"), getattr(exc, "reason", "unknown"))

    async def worker(self):
        while True:
            note = self.store.claim_note()
            if note is None:
                # Processing may have finished just before a crash and before delivery began.
                note = self.store.one("SELECT * FROM notes WHERE state IN ('ready','asr_failed') "
                                      "AND delivery='pending' AND deleted=0 ORDER BY created_at LIMIT 1")
                if note:
                    await self.deliver(note)
                else:
                    move = self.messages.pending()
                    if move and not self.note_locks[move["note_id"]].locked():
                        async with self.note_locks[move["note_id"]]:
                            await self.cleanup_move(move)
                    stale = self.store.one("SELECT * FROM notes WHERE status_message_id IS NOT NULL "
                                           "AND state IN ('ready','asr_failed') AND deleted=0 "
                                           "ORDER BY created_at LIMIT 1")
                    if stale:
                        await self.clear_status(stale)
                    await asyncio.sleep(3 if stale or move else .5)
                continue
            await self.process(note)
