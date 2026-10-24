from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

from .providers import ProviderError, silent_delivery

log = logging.getLogger(__name__)
RENEW_AFTER = 47 * 3600


class FolderViewMixin:
    """A disposable Telegram view of the archive, persisted across restarts."""

    def init_folder_view(self):
        self.view_locks = defaultdict(asyncio.Lock)
        self.view_rendering = set()
        if "view_blocked" not in {r[1] for r in self.store.db.execute("PRAGMA table_info(telegram_messages)")}:
            self.store.db.execute("ALTER TABLE telegram_messages ADD COLUMN view_blocked INTEGER NOT NULL DEFAULT 0")
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS folder_views (
                owner INTEGER PRIMARY KEY, folder_id TEXT NOT NULL, thread_id INTEGER,
                page INTEGER NOT NULL DEFAULT 0, dirty INTEGER NOT NULL DEFAULT 1
            );
        """)

    def view_busy(self, owner):
        # Do not remove originals until download/ASR/export/AI and delivery have finished.
        return self.deletion_busy(owner) or bool(self.store.one(
            "SELECT 1 FROM notes WHERE owner=? AND deleted=0 AND "
            "(state IN ('queued','processing') OR delivery IN ('pending','sending')) LIMIT 1", (owner,))) or bool(
            self.store.one("SELECT 1 FROM ai_requests WHERE owner=? AND "
                           "(state IN ('queued','running') OR delivery='pending') LIMIT 1", (owner,)))

    async def show_notes(self, owner, thread, identity, page=0, edit_id=None):
        self.store.folder(owner, identity)
        self.store.set_action(owner, None)
        self.store.select(owner, identity)
        with self.store.db:
            self.store.db.execute("INSERT INTO folder_views VALUES (?,?,?,?,1) ON CONFLICT(owner) "
                                  "DO UPDATE SET folder_id=excluded.folder_id,thread_id=excluded.thread_id,"
                                  "page=excluded.page,dirty=1", (owner, identity, thread, max(0, page)))
        if self.view_busy(owner):
            return await self.refresh_folder_keyboard(owner, thread, force=True,
                notice="Папка выбрана. Обновлю сообщения, когда завершу текущую обработку.")
        await self.render_folder_view(owner)

    async def view_send(self, method, *args, **kwargs):
        for attempt in range(4):
            try:
                return await method(*args, **kwargs)
            except ProviderError as exc:
                # 429 is an explicit refusal, so retry this operation, not the whole page.
                if str(exc) != "telegram_429" or attempt == 3:
                    raise
                await asyncio.sleep(max(1, min(60, getattr(exc, "retry_after", 5))))

    async def clear_folder_view(self, owner, thread):
        # Exact known IDs only. Leave other Telegram topics and other owners untouched.
        rows = self.store.all("SELECT message_id FROM telegram_messages WHERE owner=? AND thread_id IS ? AND view_blocked=0",
                              (owner, thread))
        leftovers = 0
        for row in rows:
            mid = row['message_id']
            if self.store.one("SELECT 1 FROM notes WHERE owner=? AND message_id=? AND deleted=0 "
                              "AND file_id IS NOT NULL AND audio_bytes=0", (owner, mid)):
                continue
            try:
                await self.view_send(self.telegram.call, 'deleteMessage', chat_id=owner, message_id=mid)
            except ProviderError as exc:
                reason = getattr(exc, 'reason', None)
                if reason == 'delete_forbidden':
                    leftovers += 1
                    with self.store.db:
                        self.store.db.execute('UPDATE telegram_messages SET view_blocked=1 '
                                              'WHERE owner=? AND message_id=?', (owner, mid))
                    continue
                elif reason != 'delete_target_missing':
                    # Retry transient/unknown failures without forgetting the deletion target.
                    raise
            with self.store.db:
                self.store.db.execute('DELETE FROM telegram_messages WHERE owner=? AND message_id=?', (owner, mid))
                self.store.db.execute('UPDATE note_messages SET deleted=1 WHERE owner=? AND message_id=?', (owner, mid))
        return leftovers

    async def render_folder_view(self, owner):
        from .bot import transcript_chunks
        view = self.store.one('SELECT * FROM folder_views WHERE owner=?', (owner,))
        if not view or self.cleanup.job(owner) or owner in self.deleting_owners:
            return
        # Folder deletion and Mini App selection may have changed the active folder.
        identity = self.store.current_folder(owner)
        page = view['page'] if identity == view['folder_id'] else 0
        thread = view['thread_id']
        count = self.store.one('SELECT count(*) AS n FROM notes WHERE owner=? AND folder_id=? AND deleted=0',
                               (owner, identity))['n']
        page = min(page, max(0, (count - 1) // 8))
        with self.store.db:
            self.store.db.execute('UPDATE folder_views SET dirty=1,folder_id=?,page=? WHERE owner=?',
                                  (identity, page, owner))
        self.view_rendering.add(owner)
        token = silent_delivery.set(True)
        try:
            leftovers = await self.clear_folder_view(owner, thread)
            notes = self.store.notes(owner, identity, offset=page * 8, limit=8)
            for note in reversed(notes):
                content = note.get('content') if note.get('content') is not None else note['transcript']
                if note['kind'] == 'photo' and note['file_id']:
                    response = await self.view_send(self.telegram.call, 'sendPhoto', chat_id=owner, photo=note['file_id'],
                        message_thread_id=thread, disable_notification=True,
                        reply_markup=self.note_controls(note, thread))
                    self.messages.record(note, response['message_id'], thread, 'view')
                for chunk in transcript_chunks(content or note['title'] or 'Запись без расшифровки'):
                    response = await self.view_send(self.telegram.text, owner, chunk, thread, self.note_controls(note, thread))
                    self.messages.record(note, response['message_id'], thread, 'view')
                request = self.ai_state.request(owner, note['id'])
                if request and request['response']:
                    for chunk in transcript_chunks('🤖 ' + request['response']):
                        response = await self.view_send(self.telegram.text, owner, chunk, thread, self.note_controls(note, thread))
                        self.ai_state.link(owner, response['message_id'], request['conversation_id'])
            self.folder_keyboards.pop(owner, None)
            await self.show_notes_menu(owner, thread, identity, page)
            if leftovers:
                await self.telegram.text(owner,
                    'Часть старых сообщений Telegram уже не разрешает удалить. '
                    'Их можно убрать вручную. Все записи сохранены в архиве.', thread)
            with self.store.db:
                self.store.db.execute('UPDATE folder_views SET dirty=0 WHERE owner=?', (owner,))
        finally:
            silent_delivery.reset(token)
            self.view_rendering.discard(owner)

    async def refresh_due_views(self, now=None):
        now = time.time() if now is None else now
        for view in self.store.all('SELECT * FROM folder_views'):
            owner = view['owner']
            if owner not in self.config.allowed_user_ids or self.view_locks[owner].locked():
                continue
            if not self.store.one('SELECT 1 FROM folders WHERE owner=?', (owner,)):
                # Delete All must never resurrect an empty folder or send a new view.
                with self.store.db:
                    self.store.db.execute('DELETE FROM folder_views WHERE owner=?', (owner,))
                continue
            oldest = self.store.one('SELECT min(coalesce(sent_at,0)) AS age FROM telegram_messages '
                                    "WHERE owner=? AND thread_id IS ? AND view_blocked=0 AND NOT EXISTS ("
                                    "SELECT 1 FROM notes n WHERE n.owner=telegram_messages.owner AND "
                                    "n.message_id=telegram_messages.message_id AND n.deleted=0 "
                                    "AND n.file_id IS NOT NULL AND n.audio_bytes=0)",
                                    (owner, view['thread_id']))['age']
            changed = self.store.current_folder(owner) != view['folder_id']
            if not view['dirty'] and not changed and (oldest is None or now - oldest < RENEW_AFTER):
                continue
            async with self.view_locks[owner]:
                if not self.view_busy(owner):
                    action = self.store.action(owner)
                    try:
                        await self.render_folder_view(owner)
                    except (ProviderError, OSError, ValueError) as exc:
                        log.warning('folder view refresh deferred owner=%s type=%s', owner, type(exc).__name__)
                    finally:
                        self.store.set_action(owner, action)

    async def folder_view_worker(self):
        while True:
            await self.refresh_due_views()
            await asyncio.sleep(5)
