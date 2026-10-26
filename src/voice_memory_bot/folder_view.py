from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict

from .delivery import DeletionOutcome, deletion_error
from .providers import ProviderError, silent_delivery

log = logging.getLogger(__name__)
RENEW_AFTER = 24 * 3600


class FolderViewMixin:
    """A disposable Telegram view of the archive, persisted across restarts."""

    def init_folder_view(self):
        self.view_locks = defaultdict(asyncio.Lock)
        self.view_rendering = set()
        self.view_refresh_event = asyncio.Event()


    def remember_screen(self, owner, thread, text, keys):
        if (owner in self.view_rendering or owner in self.deleting_owners or (self.cleanup.job(owner) or {}).get("files_pending")):
            return
        identity = self.store.current_folder(owner) or ''
        screen = json.dumps({"text": text, "keys": keys}, ensure_ascii=False)
        with self.store.transaction():
            self.store.db.execute("INSERT INTO folder_views(owner,folder_id,thread_id,page,dirty,screen) "
                                  "VALUES (?,?,?,0,0,?) ON CONFLICT(owner) DO UPDATE SET "
                                  "thread_id=excluded.thread_id,screen=excluded.screen,dirty=0",
                                  (owner, identity, thread, screen))

    def view_busy(self, owner):
        # Do not remove originals until download/ASR/export/AI and delivery have finished.
        return self.deletion_busy(owner) or bool(self.store.one(
            "SELECT 1 FROM notes WHERE owner=? AND deleted=0 AND "
            "(state IN ('queued','processing') OR delivery IN ('pending','sending')) LIMIT 1", (owner,))) or bool(
            self.store.one("SELECT 1 FROM ai_requests WHERE owner=? AND "
                           "(state IN ('queued','running') OR delivery='pending') LIMIT 1", (owner,)))

    async def show_notes(self, owner, thread, identity, page=None, edit_id=None, notice=None, command_message_id=None,
                         progress_text="Загружаю папку…"):
        self.store.folder(owner, identity)
        self.leave_folder_options(owner)
        self.store.set_action(owner, None)
        self.store.select(owner, identity)
        for index, folders in enumerate(self.folder_keyboard_pages(owner)):
            if any(folder['id'] == identity for folder in folders):
                self.folder_pages[owner] = index
                break
        with self.store.transaction():
            self.store.db.execute("INSERT INTO folder_views(owner,folder_id,thread_id,page,dirty) VALUES (?,?,?,?,1) ON CONFLICT(owner) "
                                  "DO UPDATE SET folder_id=excluded.folder_id,thread_id=excluded.thread_id,"
                                  "page=excluded.page,dirty=1,screen=NULL", (owner, identity, thread, -1 if page is None else max(-1, page)))
        with self.store.transaction():
            self.store.db.execute("UPDATE folder_views SET command_message_id=? WHERE owner=?",
                                  (command_message_id, owner))
        if self.view_busy(owner):
            self.view_refresh_event.set()
            return await self.refresh_folder_keyboard(owner, thread, force=True,
                notice="Папка выбрана. Обновлю сообщения, когда завершу текущую обработку.")
        await self.render_folder_view(owner, notice=notice, progress_text=progress_text)

    async def view_send(self, method, *args, **kwargs):
        for attempt in range(4):
            try:
                return await method(*args, **kwargs)
            except ProviderError as exc:
                # 429 is an explicit refusal, so retry this operation, not the whole page.
                if str(exc) != "telegram_429" or attempt == 3:
                    raise
                delay = max(1, min(60, getattr(exc, "retry_after", 5)))
                log.info('folder view rate limit retry attempt=%s wait_s=%s', attempt + 1, delay)
                await asyncio.sleep(delay)

    async def clear_folder_view(self, owner, thread, all_threads=False, keep_ids=()):
        # Daily renewal covers every known message of this owner, never guessed IDs or other chats.
        if all_threads:
            # Recover legacy receipts too; never enumerate adjacent Telegram IDs.
            legacy = self.store.all("SELECT message_id,thread_id FROM note_messages "
                                    "WHERE owner=? AND deleted=0 UNION SELECT message_id,NULL "
                                    "FROM ai_links a WHERE owner=? AND NOT EXISTS (SELECT 1 FROM note_messages n "
                                    "WHERE n.owner=a.owner AND n.message_id=a.message_id AND n.deleted=1)", (owner, owner))
            for receipt in legacy:
                self.cleanup.remember(owner, receipt['message_id'], receipt['thread_id'], sent_at=0)
            rows = self.store.all("SELECT message_id FROM telegram_messages WHERE owner=? AND view_blocked=0", (owner,))
        else:
            rows = self.store.all("SELECT message_id FROM telegram_messages WHERE owner=? "
                                  "AND thread_id IS ? AND view_blocked=0", (owner, thread))
        eligible = []
        for row in rows:
            mid = row['message_id']
            if mid in keep_ids:
                continue
            if self.store.one("SELECT 1 FROM notes WHERE owner=? AND message_id=? AND deleted=0 "
                              "AND file_id IS NOT NULL AND audio_bytes=0", (owner, mid)):
                continue
            eligible.append(mid)
        leftovers = 0
        for offset in range(0, len(eligible), 100):
            leftovers += await self.delete_view_batch(owner, eligible[offset:offset + 100])
        return leftovers

    async def delete_view_batch(self, owner, message_ids):
        try:
            if len(message_ids) == 1:
                await self.view_send(self.telegram.call, 'deleteMessage', chat_id=owner, message_id=message_ids[0])
            else:
                await self.view_send(self.telegram.call, 'deleteMessages', chat_id=owner, message_ids=message_ids)
        except ProviderError as exc:
            outcome = deletion_error(exc)
            if len(message_ids) > 1 and outcome in {DeletionOutcome.FORBIDDEN, DeletionOutcome.MISSING}:
                # A rejected batch can contain both deletable and expired messages.
                leftovers = 0
                for mid in message_ids:
                    leftovers += await self.delete_view_batch(owner, [mid])
                return leftovers
            if outcome == DeletionOutcome.FORBIDDEN:
                with self.store.transaction():
                    self.store.db.execute('UPDATE telegram_messages SET view_blocked=1 '
                                          'WHERE owner=? AND message_id=?', (owner, message_ids[0]))
                return 1
            if outcome != DeletionOutcome.MISSING:
                # Keep every receipt after an ambiguous network failure; deletion is idempotent.
                raise
        targets = [(owner, mid) for mid in message_ids]
        with self.store.transaction():
            self.store.db.executemany('DELETE FROM telegram_messages WHERE owner=? AND message_id=?', targets)
            self.store.db.executemany('UPDATE note_messages SET deleted=1 WHERE owner=? AND message_id=?', targets)
            # Telegram reply links are disposable; archived AI conversations remain intact.
            self.store.db.executemany('DELETE FROM ai_links WHERE owner=? AND message_id=?', targets)
        return 0

    async def render_folder_view(self, owner, *, restore_screen=False, notice=None, progress_text="Загружаю папку…"):
        from .bot import transcript_chunks
        view = self.store.one('SELECT * FROM folder_views WHERE owner=?', (owner,))
        if not view or (self.cleanup.job(owner) or {}).get('files_pending') or owner in self.deleting_owners:
            return
        # Folder deletion or another selection may have changed the active folder.
        identity = self.store.current_folder(owner)
        if identity is None:
            return await self.render_empty_view(owner, view)
        page = view['page'] if identity == view['folder_id'] else -1
        thread = view['thread_id']
        bottom = self.bottom_state(owner) if restore_screen else None
        count = self.store.one("SELECT count(*) AS n FROM notes WHERE owner=? AND folder_id=? AND deleted=0",
                               (owner, identity))['n']
        page_size = self.store.folder_page_size(owner)
        pages = max(1, (count + page_size - 1) // page_size)
        page = pages - 1 if page < 0 else min(page, pages - 1)
        with self.store.transaction():
            self.store.db.execute('UPDATE folder_views SET dirty=1,folder_id=?,page=? WHERE owner=?',
                                  (identity, page, owner))
        self.view_rendering.add(owner)
        token = silent_delivery.set(True)
        loading_id = navigation_id = None
        started = time.monotonic()
        cleanup_started = delivery_started = None
        rendered = 0
        complete = False
        try:
            folder = self.store.folder(owner, identity)
            title = f"📁 {folder['name']} (страница {page + 1} из {pages})"
            nav = []
            if page:
                nav.append({'text': '⬅️', 'callback_data': f'folder:{identity}:{page - 1}'})
            if page + 1 < pages:
                nav.append({'text': '➡️', 'callback_data': f'folder:{identity}:{page + 1}'})
            if nav:
                navigation = await self.view_send(self.telegram.text, owner, title, thread,
                    {'inline_keyboard': [nav]})
                navigation_id = navigation['message_id']
            loading = await self.view_send(self.telegram.text, owner, progress_text, thread)
            loading_id = loading["message_id"]
            self.folder_keyboards.pop(owner, None)
            header = await self.refresh_folder_keyboard(owner, thread, notice=notice,
                transient=not notice, title="☰ Меню" if nav else title)
            cleanup_started = time.monotonic()
            leftovers = await self.clear_folder_view(owner, thread, all_threads=True,
                keep_ids=(view['command_message_id'], navigation_id, loading_id, (header or {}).get('message_id')))
            delivery_started = time.monotonic()
            if restore_screen and view['screen'] and not bottom:
                screen = json.loads(view['screen'])
                # Upgrade old saved inline menus through the same bottom navigation path.
                await self.show(owner, screen['text'], thread, screen['keys'])
                with self.store.transaction():
                    self.store.db.execute('UPDATE folder_views SET dirty=0 WHERE owner=?', (owner,))
                complete = True
                return
            notes = self.store.notes(owner, identity, limit=min(page_size, max(0, count - page * page_size)),
                                     offset=max(0, count - (page + 1) * page_size))
            for note in reversed(notes):
                rendered += 1
                content = note.get('content') if note.get('content') is not None else note['transcript']
                answer = self.ai_state.result_request(owner, note['id'])
                if answer:
                    content = '🤖 ' + (content or '')
                if note['kind'] == 'photo' and note['file_id']:
                    response = await self.view_send(self.telegram.call, 'sendPhoto', chat_id=owner, photo=note['file_id'],
                        message_thread_id=thread, disable_notification=True,
                        reply_markup=self.note_controls(note, thread))
                    self.messages.record(note, response['message_id'], thread, 'view')
                for chunk in transcript_chunks(content or note['title'] or 'Запись без расшифровки'):
                    response = await self.view_send(self.telegram.text, owner, chunk, thread, self.note_controls(note, thread))
                    self.messages.record(note, response['message_id'], thread, 'view')
                    if answer:
                        self.ai_state.link(owner, response['message_id'], answer['conversation_id'])
                request = self.ai_state.request(owner, note['id'])
                if request and request['response'] and not request['result_note_id']:
                    instruction_id = response['message_id']
                    for chunk in transcript_chunks('🤖 ' + request['response']):
                        response = await self.view_send(self.telegram.text, owner, chunk, thread,
                            {'inline_keyboard': [[{'text': 'Опции', 'callback_data': f"aioptions:{note['id']}"}]]},
                            reply_to=instruction_id)
                        self.ai_state.link(owner, response['message_id'], request['conversation_id'])
            if bottom and bottom['screen'] != 'root':
                if self.store.action(owner):
                    await self.send_bottom(owner, thread, bottom)
                else:
                    await self.open_bottom_screen(owner, thread, bottom['screen'])
            if leftovers:
                await self.telegram.text(owner,
                    'Часть старых сообщений Telegram уже не разрешает удалить. '
                    'Их можно убрать вручную. Все записи сохранены в архиве.', thread)
            with self.store.transaction():
                self.store.db.execute('UPDATE folder_views SET dirty=0 WHERE owner=?', (owner,))
            complete = True
        finally:
            try:
                await self.remove_menu_message(owner, loading_id)
            finally:
                silent_delivery.reset(token)
                self.view_rendering.discard(owner)
                finished = time.monotonic()
                log.info('folder view render owner=%s page=%s notes=%s complete=%s '
                         'menu_ms=%.1f cleanup_ms=%.1f delivery_ms=%.1f total_ms=%.1f',
                         owner, page, rendered, complete,
                         ((cleanup_started or finished) - started) * 1000,
                         ((delivery_started or finished) - cleanup_started) * 1000 if cleanup_started else 0,
                         (finished - delivery_started) * 1000 if delivery_started else 0,
                         (finished - started) * 1000)

    async def render_empty_view(self, owner, view):
        self.view_rendering.add(owner)
        token = silent_delivery.set(True)
        try:
            bottom = self.bottom_state(owner)
            if bottom:
                await self.clear_folder_view(owner, view['thread_id'], all_threads=True)
                await self.send_bottom(owner, view['thread_id'], bottom)
                with self.store.transaction():
                    self.store.db.execute('UPDATE folder_views SET dirty=0,folder_id=? WHERE owner=?', ('', owner))
                return
            screen = json.loads(view['screen']) if view['screen'] else {
                'text': 'Как назовём нашу первую папку? Напиши название или пришли голосовое.',
                'keys': {'remove_keyboard': True}}
            await self.clear_folder_view(owner, view['thread_id'], all_threads=True)
            await self.view_send(self.telegram.text, owner, screen['text'], view['thread_id'], screen['keys'])
            with self.store.transaction():
                self.store.db.execute('UPDATE folder_views SET dirty=0,folder_id=? WHERE owner=?', ('', owner))
        finally:
            silent_delivery.reset(token)
            self.view_rendering.discard(owner)

    async def refresh_due_views(self, now=None):
        now = time.time() if now is None else now
        for view in self.store.all('SELECT * FROM folder_views'):
            owner = view['owner']
            # Keep the selected source and reply anchors visible throughout the conversation.
            if ((self.store.action(owner) or "").startswith("ai_note:")
                    or (self.bottom_state(owner) or {}).get("screen", "").startswith("ai_note:")):
                continue
            if owner not in self.config.allowed_user_ids or self.view_locks[owner].locked():
                continue
            if not self.store.folders(owner) and not view['screen']:
                # Delete All must never resurrect an empty folder or send a new view.
                with self.store.transaction():
                    self.store.db.execute('DELETE FROM folder_views WHERE owner=?', (owner,))
                continue
            oldest = self.store.one('SELECT min(coalesce(sent_at,0)) AS age FROM telegram_messages '
                                    "WHERE owner=? AND view_blocked=0 AND message_id IS NOT ? AND NOT EXISTS ("
                                    "SELECT 1 FROM notes n WHERE n.owner=telegram_messages.owner AND "
                                    "n.message_id=telegram_messages.message_id AND n.deleted=0 "
                                    "AND n.file_id IS NOT NULL AND n.audio_bytes=0)",
                                    (owner, view['command_message_id']))['age']
            changed = not view['screen'] and (self.store.current_folder(owner) or '') != view['folder_id']
            if not view['dirty'] and not changed and (oldest is None or now - oldest < RENEW_AFTER):
                continue
            async with self.view_locks[owner]:
                if not self.view_busy(owner):
                    action = self.store.action(owner)
                    try:
                        await self.render_folder_view(owner, restore_screen=True)
                    except (ProviderError, OSError, ValueError) as exc:
                        log.warning('folder view refresh deferred owner=%s type=%s', owner, type(exc).__name__)
                    finally:
                        self.store.set_action(owner, action)

    async def folder_view_worker(self):
        while True:
            self.view_refresh_event.clear()
            await self.refresh_due_views()
            # Completion signals wake immediately; the short dirty fallback covers external uploads.
            timeout = 1 if self.store.one('SELECT 1 FROM folder_views WHERE dirty=1 LIMIT 1') else 5
            try:
                await asyncio.wait_for(self.view_refresh_event.wait(), timeout)
            except TimeoutError:
                pass
