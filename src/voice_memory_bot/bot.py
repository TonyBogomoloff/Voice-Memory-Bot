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

from .archive import Archive
from .config import Config
from .message_store import MessageStore
from .providers import CodexRouter, Groq, ProviderError, Telegram
from .routing import decide
from .store import Store, clean_name

log = logging.getLogger(__name__)


def button(text, data):
    return {"text": text, "callback_data": data}


def keyboard(*rows):
    return {"inline_keyboard": list(rows)}


def options_keyboard(identity):
    return keyboard([button("Опции", f"options:{identity}")])


def missing_topic(error):
    return getattr(error, "reason", None) in {"thread_not_found", "thread_invalid", "topic_deleted"}


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


HOME = keyboard([button("📁 Папки", "folders"), button("🔎 Поиск", "search")],
                [button("📦 Экспорт всего", "export:all"), button("ℹ️ Помощь", "help")])

HELP = """🎙 Voice Memory Bot

Выбери тему проекта и отправь голосовое или текст. Можно сказать в начале или конце записи:
«Сохрани в проект Работа» — эта команда важнее выбранной темы.
Без команды запись останется в выбранном проекте. Неоднозначные указания попадут во «Входящие».

/folders — проекты и записи
/new Название — создать проект
/notes — последние записи
/search текст — поиск по всему архиву
/export — выгрузить архив
/cancel — отменить ввод названия или поиск

В карточке записи доступны текст, исходное аудио, перенос и удаление.
Голосовые сразу расшифровываются в текст. Под расшифровкой: «Опции» → «Переместить».
Фото тоже сохраняются: под снимком появится отдельное сообщение с кнопкой «Опции».
Аудио отправляется в Groq для распознавания, текст — в OpenAI для выбора проекта и заголовка.
Оригинал и исходная расшифровка хранятся в твоём архиве. Удаление из архива не удаляет сообщения
в истории Telegram. Автосинхронизация с другими сервисами пока не подключена."""


class Bot:
    def __init__(self, config: Config, store: Store, telegram: Telegram, groq: Groq, router: CodexRouter):
        self.config, self.store = config, store
        self.telegram, self.groq, self.router = telegram, groq, router
        self.archive = Archive(config.data_dir / "notes")
        self.messages = MessageStore(store)
        self.topics_enabled = False
        self.recent = defaultdict(deque)
        self.export_locks = defaultdict(asyncio.Lock)
        self.note_locks = defaultdict(asyncio.Lock)
        self.folder_locks = defaultdict(asyncio.Lock)
        self.export_tasks = set()

    async def start(self):
        me = await self.telegram.call("getMe")
        self.topics_enabled = bool(me.get("has_topics_enabled"))
        webhook = await self.telegram.call("getWebhookInfo")
        if webhook.get("url"):
            raise ProviderError("webhook_present_stop_before_polling")
        await self.telegram.call("setMyCommands", commands=[
            {"command": "start", "description": "Открыть Voice Memory"},
            {"command": "folders", "description": "Папки проектов"},
            {"command": "new", "description": "Создать папку"},
            {"command": "notes", "description": "Последние записи"},
            {"command": "search", "description": "Поиск по записям"},
            {"command": "export", "description": "Выгрузить архив"},
            {"command": "help", "description": "Как пользоваться"},
            {"command": "cancel", "description": "Отменить ввод"},
        ])
        # Finish physical deletion interrupted by a previous shutdown.
        for note in self.store.all("SELECT * FROM notes WHERE deleted=1"):
            self.archive.delete(note)
        log.info("started bot_id=%s topics=%s", me["id"], self.topics_enabled)
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self.poll())
                group.create_task(self.dispatch())
                group.create_task(self.worker())
        finally:
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
        self.store.ensure_user(owner)
        thread = message.get("message_thread_id")
        if callback:
            await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"])
        if (callback or message.get("text", "").startswith("/")) and not self.rate_allowed(owner):
            return
        try:
            if callback:
                return await self.callback(owner, thread, message["message_id"], callback.get("data", ""))
            if "forum_topic_created" in message:
                topic = message["forum_topic_created"]
                if thread and topic.get("is_name_implicit"):
                    self.store.mark_implicit_topic(owner, thread)
                    return
                if thread and not self.store.folder_for_thread(owner, thread):
                    name = topic.get("name") or f"Проект {thread}"
                    self.store.import_topic(owner, name, thread)
                return
            if "forum_topic_edited" in message:
                folder = self.store.folder_for_thread(owner, thread)
                name = message["forum_topic_edited"].get("name")
                if folder and name:
                    self.store.rename_folder(owner, folder["id"], name)
                elif thread and name:
                    self.store.import_topic(owner, name, thread)
                return
            if thread and not self.store.folder_for_thread(owner, thread) and not self.store.is_implicit_topic(owner, thread):
                self.store.create_folder(owner, f"Проект {thread}", thread)
            text = message.get("text", "").strip()
            if text.startswith("/"):
                command, _, argument = text.partition(" ")
                return await self.command(owner, thread, command.split("@")[0].lower(), argument.strip())
            if text and self.store.action(owner):
                return await self.input_action(owner, thread, text)
            folder = self.store.current_folder(owner, thread)
            if text:
                note, created = self.store.add_note(owner, folder, message, "text", transcript=text)
            else:
                kind = next((k for k in ("voice", "audio", "document", "photo") if message.get(k)), None)
                if kind is None:
                    return await self.show(owner, "Пришли голосовое, аудиофайл, фото или текстовую заметку.", thread, HOME)
                media = message[kind][-1] if kind == "photo" else message[kind]
                extension = ("jpg" if kind == "photo" else "ogg" if kind == "voice"
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
                    response = await self.telegram.text(owner, status, thread)
                    self.store.update_note(owner, note["id"], status_message_id=response["message_id"])
        except ValueError as exc:
            await self.show(owner, str(exc), thread, HOME)

    async def command(self, owner, thread, command, argument):
        if command in {"/start", "/help", "/privacy"}:
            if command == "/start":
                try:
                    await self.ensure_topic(owner, self.store.ensure_user(owner))
                except ProviderError:
                    pass
            return await self.show(owner, HELP, thread, HOME)
        if command == "/cancel":
            self.store.set_action(owner, None)
            return await self.show(owner, "Ввод отменён.", thread, HOME)
        if command == "/folders":
            return await self.show_folders(owner, thread)
        if command == "/new":
            if argument:
                return await self.create_folder(owner, thread, argument)
            self.store.set_action(owner, "create")
            return await self.show(owner, "Пришли название новой папки текстом. /cancel — отмена.", thread)
        if command == "/notes":
            return await self.show_notes(owner, thread, self.store.current_folder(owner, thread))
        if command == "/search":
            if argument:
                return await self.search(owner, thread, argument)
            self.store.set_action(owner, "search")
            return await self.show(owner, "Что найти в твоих заметках? Пришли слова текстом.", thread)
        if command == "/export":
            return await self.export_menu(owner, thread, "all")
        return await self.show(owner, "Команда не найдена. Открой меню папок или отправь заметку.", thread, HOME)

    async def input_action(self, owner, thread, text):
        action = self.store.action(owner)
        if action == "create":
            await self.create_folder(owner, thread, text)
        elif action == "search":
            await self.search(owner, thread, text)
        elif action.startswith("rename:"):
            identity = action.split(":")[1]
            folder = self.store.folder(owner, identity)
            name = clean_name(text)
            self.store.check_names(owner, [name, *json.loads(folder["aliases"])], identity)
            if folder["thread_id"]:
                await self.telegram.call("editForumTopic", chat_id=owner, message_thread_id=folder["thread_id"], name=name)
            self.store.rename_folder(owner, identity, name)
            await self.show_notes(owner, thread, identity)
        elif action.startswith("aliases:"):
            identity = action.split(":")[1]
            aliases = [] if text == "-" else [s.strip() for s in text.split(",") if s.strip()]
            self.store.aliases(owner, identity, aliases)
            await self.show_notes(owner, thread, identity)
        self.store.set_action(owner, None)

    async def ensure_topic(self, owner, identity, invalid_thread=None):
        async with self.folder_locks[identity]:
            folder = self.store.folder(owner, identity)
            if invalid_thread is not None and folder["thread_id"] == invalid_thread:
                self.store.bind_thread(owner, identity, None)
                folder["thread_id"] = None
            if self.topics_enabled and not folder["thread_id"]:
                result = await self.telegram.call("createForumTopic", chat_id=owner, name=folder["name"])
                self.store.bind_thread(owner, identity, result["message_thread_id"])
                folder = self.store.folder(owner, identity)
            return folder["thread_id"]

    async def create_folder(self, owner, thread, name, note_id=None):
        folder = self.store.create_folder(owner, name)
        self.store.select(owner, folder["id"])
        try:
            await self.ensure_topic(owner, folder["id"])
        except ProviderError:
            await self.show(owner, "Папка создана в архиве. Тему Telegram пока создать не удалось.", thread)
        if note_id:
            await self.move(owner, note_id, folder["id"], thread)
        else:
            await self.show_notes(owner, thread, folder["id"])

    async def show_folders(self, owner, thread=None, edit_id=None, page=0, note_id=None):
        folders = self.store.folders(owner)
        rows = []
        page = max(0, page)
        for folder in folders[page * 8:(page + 1) * 8]:
            action = f"move:{note_id}:{folder['id']}" if note_id else f"folder:{folder['id']}:0"
            rows.append([button(f"📁 {folder['name']} · {folder['count']}", action)])
        nav = []
        mode = f"choose:{note_id}" if note_id else "folders"
        if page:
            nav.append(button("←", f"{mode}:{page - 1}"))
        if len(folders) > (page + 1) * 8:
            nav.append(button("→", f"{mode}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([button("＋ Новая папка", "create"), button("🔎 Поиск", "search")])
        await self.show(owner, "Выбери папку для записи:" if note_id else "Твои проекты:",
                        thread, keyboard(*rows), edit_id)

    async def show_notes(self, owner, thread, identity, page=0, edit_id=None):
        folder = self.store.folder(owner, identity)
        notes = self.store.notes(owner, identity, offset=page * 8, limit=9)
        aliases = ", ".join(json.loads(folder["aliases"]))
        text = f"📁 {folder['name']}" + (f"\nМожно говорить: {aliases}" if aliases else "")
        text += "\nЗаписи:" if notes else "\nПока нет записей. Отправь голосовое в эту тему или выбери папку ниже."
        rows = [[button(n["title"][:55], f"note:{n['id']}")] for n in notes[:8]]
        nav = []
        if page:
            nav.append(button("←", f"folder:{identity}:{page - 1}"))
        if len(notes) > 8:
            nav.append(button("→", f"folder:{identity}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.extend([[button("Сохранять сюда", f"select:{identity}"), button("📦 Экспорт", f"export:{identity}")],
                     [button("Псевдонимы", f"aliases:{identity}")]])
        if not folder["is_inbox"]:
            rows[-1].append(button("Переименовать", f"rename:{identity}"))
        rows.append([button("← Папки", "folders")])
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
        if note["transcript"]:
            text += "\n\n" + note["transcript"][:450]
            if len(note["transcript"]) > 450:
                text += "…"
        identity = note["id"]
        rows = [[button("📄 Текст", f"text:{identity}"), button("📁 Переместить", f"choose:{identity}:0")]]
        if note["audio_bytes"]:
            rows.append([button("📷 Фото" if note["kind"] == "photo" else "🎙 Аудио", f"audio:{identity}")])
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
        pieces = data.split(":")
        action, identity = pieces[0], pieces[1] if len(pieces) > 1 else None
        if action == "help":
            return await self.show(owner, HELP, thread, HOME, message_id)
        if action in {"create", "search"}:
            return await self.command(owner, thread, "/new" if action == "create" else "/search", "")
        if action == "folders":
            return await self.show_folders(owner, thread, message_id, int(identity or 0))
        if action == "folder":
            return await self.show_notes(owner, thread, identity, int(pieces[2]), message_id)
        if action == "select":
            self.store.select(owner, identity)
            await self.ensure_topic(owner, identity)
            name = self.store.folder(owner, identity)["name"]
            return await self.show(owner, f"Выбрана папка «{name}» для сообщений из общего чата.\n"
                                   "В теме проекта записи сохраняются в эту тему, если голосом не указано другое.",
                                   thread, HOME, message_id)
        if action in {"rename", "aliases"}:
            self.store.folder(owner, identity)
            self.store.set_action(owner, data)
            prompt = "Пришли новое название." if action == "rename" else "Пришли названия через запятую. «-» удалит псевдонимы."
            return await self.show(owner, prompt + " /cancel — отмена.", thread)
        if action == "export":
            return await self.export_menu(owner, thread, identity, message_id)
        if action == "zip":
            folder_id = None if identity == "all" else identity
            if folder_id:
                self.store.folder(owner, folder_id)
            task = asyncio.create_task(self.send_export(owner, thread, folder_id, pieces[2] == "audio"))
            self.export_tasks.add(task)
            task.add_done_callback(self.export_tasks.discard)
            return
        note = self.store.note(owner, identity)
        if action == "options":
            return await self.edit_note_buttons(owner, message_id, keyboard(
                [button("Переместить", f"pick:{identity}:0")],
                [button("← Назад", f"collapse:{identity}")]))
        if action == "collapse":
            return await self.edit_note_buttons(owner, message_id, options_keyboard(identity))
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
        if action == "text":
            if note["kind"] == "photo" and not note["transcript"]:
                return await self.show(owner, "У фото нет подписи.", thread)
            if note["transcript"] is None:
                raise ValueError("Расшифровка ещё не получена; исходное аудио можно выгрузить отдельно")
            if len(note["transcript"]) <= 3900:
                return await self.show(owner, note["transcript"], thread)
            self.archive.persist_text(note, self.store.folder(owner, note["folder_id"]))
            return await self.telegram.document(owner, self.archive.directory(note) / "transcript.txt", thread)
        if action == "audio":
            path = self.archive.audio(note)
            if not note["audio_bytes"] or not path.exists():
                raise ValueError("Исходный файл пока недоступен")
            return await self.telegram.document(owner, path, thread,
                                                "Фото" if note["kind"] == "photo" else "Исходное аудио без перекодирования")
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

    async def export_menu(self, owner, thread, identity, edit_id=None):
        label = "всего архива" if identity == "all" else "папки «" + self.store.folder(owner, identity)["name"] + "»"
        await self.show(owner, f"Экспорт {label}:", thread, keyboard(
            [button("Только тексты", f"zip:{identity}:text"), button("Тексты + файлы", f"zip:{identity}:audio")],
            [button("← Папки", "folders")]), edit_id)

    async def send_export(self, owner, thread, identity, audio):
        if self.export_locks[owner].locked():
            return
        async with self.export_locks[owner]:
            try:
                notes = self.store.notes(owner, identity, limit=self.config.limits.max_notes_per_user)
                if not notes:
                    return await self.show(owner, "В архиве пока нет записей.", thread)
                await self.show(owner, "Готовлю ZIP. Большой архив будет разделён на части.", thread)
                export_root = self.config.data_dir / "exports"
                export_root.mkdir(exist_ok=True, mode=0o700)
                with tempfile.TemporaryDirectory(prefix="export-", dir=export_root) as directory:
                    # Snapshot metadata on the event loop; file work runs in a thread.
                    folders = self.store.folders(owner)
                    paths = await asyncio.to_thread(self.archive.export, notes, folders, Path(directory),
                                                    audio, self.config.limits.export_part_bytes)
                    for i, path in enumerate(paths):
                        await self.telegram.document(owner, path, thread, f"Архив · часть {i + 1} из {len(paths)}")
            except (ValueError, OSError) as exc:
                log.warning("export failed type=%s", type(exc).__name__)
                await self.show(owner, "Не удалось собрать полный архив. Попробуй ещё раз; записи сохранены.", thread)
            except ProviderError as exc:
                log.warning("export delivery error=%s", exc)

    async def move(self, owner, identity, folder_id, thread):
        note = self.store.note(owner, identity)
        folder = self.store.folder(owner, folder_id)
        if note["state"] in {"processing", "queued"} or self.note_locks[identity].locked():
            raise ValueError("Дождись завершения обработки, затем перенеси запись")
        async with self.note_locks[identity]:
            previous = self.messages.get(owner, identity)
            if previous and previous["state"] == "cleanup":
                return await self.cleanup_move(previous)
            if folder_id == note["folder_id"] and thread == folder["thread_id"]:
                return await self.show(owner, "Запись уже в этой папке.", thread)
            self.messages.begin(note, folder_id, thread)
            target_note = {**note, "folder_id": folder_id, "routing": "manual", "requested_folder": None}
            try:
                destination = await self.ensure_topic(owner, folder_id)
                response, destination = await self.publish_note(target_note, destination)
                self.archive.persist_text(target_note, self.store.folder(owner, folder_id))
                # Destination is confirmed before the source becomes eligible for deletion.
                self.messages.confirm(note, folder_id, response, destination)
            except (ProviderError, OSError) as exc:
                self.messages.state(owner, identity, "unknown")
                log.warning("move delivery not confirmed note_id=%s type=%s", identity, type(exc).__name__)
                return await self.show(owner, "Не удалось подтвердить перенос. Исходные сообщения сохранены.", thread)
            return await self.cleanup_move(self.messages.get(owner, identity))

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
            return
        self.messages.state(owner, identity, "blocked" if forbidden else "done")
        text = ("Запись перенесена, но Telegram не разрешил удалить часть исходных сообщений."
                if forbidden else "Запись перенесена.")
        try:
            await self.show(owner, text, move["source_thread"])
        except ProviderError as exc:
            log.warning("move confirmation not delivered note_id=%s error=%s", identity, exc)

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
                # A folder/title decision must never delay the text the user is waiting for.
                await self.deliver_transcript(note)
                folders = self.store.folders(owner)
                result = None
                try:
                    result = await self.router.classify(note["transcript"], folders)
                except ProviderError as exc:
                    log.warning("routing error=%s", exc)
                decision = decide(note["transcript"], folders, note["source_folder"],
                                  self.store.ensure_user(owner), result)
                self.store.update_note(owner, identity, state="ready", title=decision.title,
                    folder_id=decision.folder_id, routing=decision.status, requested_folder=decision.requested_folder)
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
        keys = keyboard([button("Повторить", f"retry:{note['id']}")]) if error else options_keyboard(note["id"])
        self.store.update_note(note["owner"], note["id"], transcript_delivery="sending")
        try:
            response = await self.show(note["owner"], text, note["source_thread"], keys, edit_id)
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
        self.store.update_note(owner, identity, status_message_id=None)
        self.messages.removed(owner, identity, message_id)

    async def send_transcript(self, note, thread, reply_to=None, recover_topic=False):
        chunks = list(transcript_chunks(note["transcript"]))
        for index, text in enumerate(chunks):
            keys = options_keyboard(note["id"]) if index == len(chunks) - 1 else None
            try:
                response = await self.telegram.text(note["owner"], text, thread, keys, reply_to=reply_to)
            except ProviderError as exc:
                # Only a definitive rejection before any published part permits another send.
                if not (recover_topic and index == 0 and thread and self.topics_enabled and missing_topic(exc)):
                    raise
                thread = await self.ensure_topic(note["owner"], note["folder_id"], invalid_thread=thread)
                response = await self.telegram.text(note["owner"], text, thread, keys, reply_to=reply_to)
            self.messages.record(note, response["message_id"], thread, "transcript")
        return response, thread

    async def publish_note(self, note, thread):
        if note["kind"] == "photo":
            try:
                response = await self.telegram.call("sendPhoto", chat_id=note["owner"], message_thread_id=thread,
                    photo=note["file_id"], caption=note["transcript"] or None, reply_markup=options_keyboard(note["id"]))
            except ProviderError as exc:
                if not (thread and self.topics_enabled and missing_topic(exc)):
                    raise
                thread = await self.ensure_topic(note["owner"], note["folder_id"], invalid_thread=thread)
                response = await self.telegram.call("sendPhoto", chat_id=note["owner"], message_thread_id=thread,
                    photo=note["file_id"], caption=note["transcript"] or None, reply_markup=options_keyboard(note["id"]))
            self.messages.record(note, response["message_id"], thread, "photo")
            return response, thread
        if note["file_id"]:
            method, field = {"voice": ("sendVoice", "voice"), "audio": ("sendAudio", "audio"),
                             "document": ("sendDocument", "document")}[note["kind"]]
            keys = options_keyboard(note["id"]) if not note["transcript"] else None
            try:
                response = await self.telegram.call(method, chat_id=note["owner"], message_thread_id=thread,
                                                     reply_markup=keys, **{field: note["file_id"]})
            except ProviderError as exc:
                if not (thread and self.topics_enabled and missing_topic(exc)):
                    raise
                thread = await self.ensure_topic(note["owner"], note["folder_id"], invalid_thread=thread)
                response = await self.telegram.call(method, chat_id=note["owner"], message_thread_id=thread,
                                                     reply_markup=keys, **{field: note["file_id"]})
            self.messages.record(note, response["message_id"], thread, "audio")
            if not note["transcript"]:
                return response, thread
        return await self.send_transcript(note, thread, recover_topic=not note["file_id"])

    async def deliver_transcript(self, note):
        owner, identity = note["owner"], note["id"]
        note = self.store.note(owner, identity)
        if not note["transcript"] or note["transcript_delivery"] != "pending":
            return
        await self.clear_status(note)
        self.store.update_note(owner, identity, transcript_delivery="sending")
        try:
            response, _ = await self.send_transcript(note, note["source_thread"])
            self.store.update_note(owner, identity, transcript_delivery="sent",
                                   transcript_message_id=response["message_id"])
        except ProviderError as exc:
            self.store.update_note(owner, identity, transcript_delivery="unknown")
            log.warning("transcript delivery unknown note_id=%s error=%s", identity, exc)

    async def deliver(self, note):
        owner, identity = note["owner"], note["id"]
        if self.store.note(owner, identity)["delivery"] != "pending":
            return
        # Mark intent before any non-idempotent Telegram operation, including topic creation.
        self.store.update_note(owner, identity, delivery="sending")
        try:
            await self.clear_status(note)
            if not note["transcript"] and note["kind"] != "photo":
                text = "Не удалось конвертировать запись в текст. Попробуй ещё раз."
                keys = keyboard([button("Повторить", f"retry:{identity}")])
                response = await self.telegram.text(owner, text, note["source_thread"], keys)
                self.store.update_note(owner, identity, delivery="sent", result_message_id=response["message_id"],
                                       result_thread=note["source_thread"])
                return
            if note["kind"] != "photo":
                await self.deliver_transcript(note)
            thread = await self.ensure_topic(owner, note["folder_id"])
            if not thread or thread == note["source_thread"]:
                saved = self.store.note(owner, identity)
                self.store.update_note(owner, identity, delivery=saved["transcript_delivery"],
                                       result_message_id=saved["transcript_message_id"],
                                       result_thread=note["source_thread"])
                return
            response, thread = await self.publish_note(note, thread)
            self.store.update_note(owner, identity, delivery="sent", result_message_id=response["message_id"],
                                   result_thread=thread)
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
