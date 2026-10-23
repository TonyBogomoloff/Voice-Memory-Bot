from __future__ import annotations

import logging

from .providers import ProviderError
from .store import clean_name, normalize

log = logging.getLogger(__name__)


def record_button(thread):
    return {"text": "✍️ Записать сюда", "callback_data": f"record:{thread}"}


class RecordingMixin:
    def init_recording(self):
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS record_panels (
                owner INTEGER NOT NULL REFERENCES users(id), thread_id INTEGER NOT NULL,
                message_id INTEGER, state TEXT NOT NULL, PRIMARY KEY(owner,thread_id)
            );
            CREATE TABLE IF NOT EXISTS record_prompts (
                owner INTEGER NOT NULL REFERENCES users(id), message_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL, purpose TEXT NOT NULL, PRIMARY KEY(owner,message_id)
            );
            UPDATE record_panels SET state='unknown' WHERE state='sending';
        """)

    def note_controls(self, note, thread):
        row = [{"text": "Опции", "callback_data": f"options:{note['id']}"}]
        if thread:
            row.insert(0, record_button(thread))
        return {"inline_keyboard": [row]}

    async def install_record_panels(self, owner=None):
        if not self.topics_enabled:
            return
        for user in sorted(self.config.allowed_user_ids if owner is None else {owner}):
            job = self.cleanup.job(user)
            if job or user in self.deleting_owners:
                continue
            topics = self.store.all(
                "SELECT thread_id FROM folders WHERE owner=? AND thread_id IS NOT NULL "
                "UNION SELECT thread_id FROM implicit_topics WHERE owner=?", (user, user))
            for topic in topics:
                await self.ensure_record_panel(user, topic["thread_id"])

    async def ensure_record_panel(self, owner, thread):
        if not self.topics_enabled or not thread:
            return
        async with self.folder_locks[f"record:{owner}:{thread}"]:
            if self.store.one("SELECT 1 FROM record_panels WHERE owner=? AND thread_id=?", (owner, thread)):
                return
            folder = self.store.folder_for_thread(owner, thread)
            text = f"📁 {folder['name']}" if folder else "📁 Записи в этой папке"
            with self.store.db:
                self.store.db.execute("INSERT INTO record_panels VALUES (?,?,NULL,'sending')", (owner, thread))
            try:
                response = await self.telegram.call("sendMessage", chat_id=owner, message_thread_id=thread,
                    text=text, disable_notification=True, reply_markup={"inline_keyboard": [[record_button(thread)]]})
                if response.get("message_thread_id", thread) != thread:
                    raise ProviderError("telegram_topic_mismatch")
            except ProviderError as exc:
                with self.store.db:
                    self.store.db.execute("UPDATE record_panels SET state='unknown' WHERE owner=? AND thread_id=?",
                                          (owner, thread))
                log.warning("record panel unconfirmed thread=%s error=%s", thread, exc)
                return
            with self.store.db:
                self.store.db.execute("UPDATE record_panels SET message_id=?,state='sent' "
                                      "WHERE owner=? AND thread_id=?", (response["message_id"], owner, thread))

    async def start_recording(self, owner, thread):
        folder = self.store.folder_for_thread(owner, thread)
        panel = self.store.one("SELECT 1 FROM record_panels WHERE owner=? AND thread_id=? AND state='sent'",
                               (owner, thread))
        if not folder and not panel:
            raise ValueError("Папка не найдена")
        if self.store.one("SELECT 1 FROM deletion_targets WHERE owner=? AND kind='topic' AND identity=?",
                          (owner, thread)):
            raise ValueError("Эта папка ожидает удаления")
        purpose = "record" if folder else "name"
        text = (f"📁 {folder['name']}\nЗапиши голосовое или пришли текст/фото."
                if folder else "Как называется эта папка? Пришли название текстом один раз, чтобы подключить её.")
        placeholder = (f"Запись в {folder['name']}" if folder else "Название этой папки")[:64]
        response = await self.telegram.text(owner, text, thread,
            {"force_reply": True, "input_field_placeholder": placeholder})
        if response.get("message_thread_id", thread) != thread:
            raise ProviderError("telegram_topic_mismatch")
        with self.store.db:
            self.store.db.execute("INSERT INTO record_prompts VALUES (?,?,?,?)",
                                  (owner, response["message_id"], thread, purpose))
        log.info("record prompt ready message_id=%s thread=%s purpose=%s", response["message_id"], thread, purpose)

    def recording_reply(self, owner, message):
        reply = message.get("reply_to_message", {})
        if reply.get("chat", {}).get("id", owner) != owner:
            return None
        prompt = self.store.one("SELECT * FROM record_prompts WHERE owner=? AND message_id=? "
                                "AND purpose IN ('record','name')", (owner, reply.get("message_id")))
        # A real newly selected topic takes precedence over an old reply draft.
        if prompt and message.get("message_thread_id") not in (None, prompt["thread_id"]):
            return None
        return prompt

    async def name_recording_folder(self, owner, prompt, message):
        text = message.get("text", "").strip()
        if not text:
            await self.telegram.text(owner, "Сначала пришли название папки текстом ответом на запрос.",
                                     prompt["thread_id"], reply_to=prompt["message_id"])
            return
        name = clean_name(text)
        folder = self.store.folder_for_thread(owner, prompt["thread_id"])
        if not folder:
            existing = next((f for f in self.store.folders(owner) if normalize(f["name"]) == normalize(name)), None)
            if existing:
                await self.telegram.text(owner,
                    f"Архив «{existing['name']}» уже существует. Подключить его к этой папке? "
                    "Все записи сохранятся. Прежняя привязка будет заменена, темы Telegram не удаляются.",
                    prompt["thread_id"], {"inline_keyboard": [[{
                        "text": "Подключить существующий архив",
                        "callback_data": f"recordlink:{prompt['message_id']}:{existing['id']}"}]]})
                return
            self.store.check_names(owner, [name])
            folder = self.store.import_topic(owner, name, prompt["thread_id"])
        with self.store.db:
            self.store.db.execute("UPDATE record_prompts SET purpose='done' WHERE owner=? AND message_id=?",
                                  (owner, prompt["message_id"]))
        panel = self.store.one("SELECT message_id FROM record_panels WHERE owner=? AND thread_id=?",
                               (owner, prompt["thread_id"]))
        if panel and panel["message_id"]:
            try:
                await self.show(owner, f"📁 {folder['name']}", prompt["thread_id"],
                                {"inline_keyboard": [[record_button(prompt["thread_id"])]]}, panel["message_id"])
            except ProviderError:
                log.warning("record panel rename unconfirmed thread=%s", prompt["thread_id"])
        await self.start_recording(owner, prompt["thread_id"])

    async def link_recording_folder(self, owner, prompt_id, folder_id):
        async with self.folder_locks[f"record-link:{owner}"]:
            prompt = self.store.one("SELECT * FROM record_prompts WHERE owner=? AND message_id=? AND purpose='name'",
                                    (owner, prompt_id))
            if not prompt:
                raise ValueError("Этот запрос уже завершён. Нажми «Записать сюда» ещё раз.")
            folder = self.store.folder(owner, folder_id)
            if folder["is_inbox"] or self.store.folder_for_thread(owner, prompt["thread_id"]):
                raise ValueError("Эта папка уже подключена или является папкой «Входящие».")
            old_thread = folder["thread_id"]
            with self.store.db:
                self.store.db.execute("UPDATE folders SET thread_id=? WHERE owner=? AND id=?",
                                      (prompt["thread_id"], owner, folder_id))
                self.store.db.execute("UPDATE record_prompts SET purpose='done' WHERE owner=? AND thread_id IN (?,?)",
                                      (owner, old_thread, prompt["thread_id"]))
                self.store.db.execute("UPDATE record_panels SET state='detached' WHERE owner=? AND thread_id=?",
                                      (owner, old_thread))
            panel = self.store.one("SELECT message_id FROM record_panels WHERE owner=? AND thread_id=?",
                                   (owner, prompt["thread_id"]))
            if panel and panel["message_id"]:
                try:
                    await self.show(owner, f"📁 {folder['name']}", prompt["thread_id"],
                                    {"inline_keyboard": [[record_button(prompt["thread_id"])]]}, panel["message_id"])
                except ProviderError:
                    log.warning("record panel refresh failed thread=%s", prompt["thread_id"])
            await self.start_recording(owner, prompt["thread_id"])
