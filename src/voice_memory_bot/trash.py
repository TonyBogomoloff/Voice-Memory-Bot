"""Permanent, snapshot-scoped trash cleanup with durable exact-message receipts."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict

from .delivery import DeletionOutcome, delete_message, deletion_error
from .providers import ProviderError
from .store import new_id

log = logging.getLogger(__name__)
TRASH_DESCRIPTION = 'Это системная папка. Сюда попадают удалённые сообщения.'


def folder_icon(folder, active=False):
    return '🗑' if folder['is_trash'] else '✅' if active else '📁'


class TrashStore:
    def __init__(self, store):
        self.store, self.db = store, store.db

    def snapshot(self, owner, folder_id):
        folder = self.store.folder(owner, folder_id)
        if not folder['is_trash'] or folder['removed']:
            raise ValueError('Очистить можно только системную папку «Корзина»')
        return self.store.all('SELECT * FROM notes WHERE owner=? AND folder_id=? ORDER BY id', (owner, folder_id))

    def begin(self, owner, folder_id, identities):
        """Recheck membership and commit erasure and cleanup intent in one transaction."""
        with self.store.transaction():
            wanted = set(identities)
            notes = [note for note in self.snapshot(owner, folder_id) if note['id'] in wanted]
            targets = {note['id'] for note in notes}
            if any(note['state'] in {'queued', 'processing'} or note['delivery'] == 'sending'
                   or note['transcript_delivery'] == 'sending' for note in notes if not note['deleted']):
                raise ValueError('Дождись завершения обработки, затем очисти корзину')
            if self.store.one("SELECT 1 FROM ai_requests WHERE owner=? AND state!='deleted' AND "
                              "(state IN ('queued','running') OR delivery IN ('pending','sending'))", (owner,)):
                raise ValueError('Дождись завершения обработки ИИ, затем очисти корзину')
            # A legacy field can also point at an AI result that was moved elsewhere.
            # Explicit ownership by any surviving note always excludes that message.
            protected = set()
            for note in self.store.all('SELECT * FROM notes WHERE owner=?', (owner,)):
                if note['id'] not in targets:
                    protected.update(note.get(field) for field in (
                        'message_id', 'status_message_id', 'transcript_message_id', 'result_message_id'))
            for receipt in self.store.all('SELECT message_id,note_id FROM note_messages WHERE owner=?', (owner,)):
                if receipt['note_id'] not in targets:
                    protected.add(receipt['message_id'])
            queued = set()
            for note in notes:
                identity = note['id']
                receipts = self.store.all('SELECT message_id,deleted FROM note_messages WHERE owner=? AND note_id=?',
                                          (owner, identity))
                removed = {r['message_id'] for r in receipts if r['deleted']}
                messages = {r['message_id'] for r in receipts if not r['deleted']}
                messages.update(note.get(field) for field in (
                    'message_id', 'status_message_id', 'transcript_message_id', 'result_message_id'))
                progress = self.store.one('SELECT message_id FROM ai_progress WHERE note_id=?', (identity,))
                if progress:
                    messages.add(progress['message_id'])
                messages = {mid for mid in messages if type(mid) is int and mid > 0} - removed - protected - queued
                queued.update(messages)
                self.db.execute('INSERT INTO trash_cleanup(owner,note_id,message_ids) VALUES (?,?,?)',
                                (owner, identity, json.dumps(sorted(messages))))
                for table in ('ai_message_jobs', 'ai_note_jobs'):
                    self.db.execute(f'DELETE FROM {table} WHERE owner=? AND (note_id=? OR request_id=?)',
                                    (owner, identity, identity))
                for table in ('note_revisions', 'ai_progress', 'ai_steps'):
                    self.db.execute(f'DELETE FROM {table} WHERE note_id=?', (identity,))
                for table in ('ai_note_context', 'ai_requests', 'note_moves', 'note_messages', 'reply_moves'):
                    self.db.execute(f'DELETE FROM {table} WHERE owner=? AND note_id=?', (owner, identity))
                # Keep result_note_id as a tombstone: clearing it would recreate the
                # deleted answer at startup. Remove only the duplicate answer text.
                self.db.execute('UPDATE ai_requests SET response=NULL WHERE owner=? AND result_note_id=?',
                                (owner, identity))
                self.db.executemany('DELETE FROM ai_links WHERE owner=? AND message_id=?',
                                    [(owner, mid) for mid in messages])
                self.db.execute('DELETE FROM notes WHERE owner=? AND folder_id=? AND id=?',
                                (owner, folder_id, identity))
            return len(notes)

    def jobs(self, owner):
        return self.store.all('SELECT * FROM trash_cleanup WHERE owner=?', (owner,))

    def pending(self, owner):
        return any(row['files_pending'] or json.loads(row['message_ids']) for row in self.jobs(owner))

    def save(self, owner, identity, pending, blocked):
        with self.store.transaction():
            self.db.execute('UPDATE trash_cleanup SET message_ids=?,blocked_ids=? WHERE owner=? AND note_id=?',
                            (json.dumps(pending), json.dumps(blocked), owner, identity))
            self.db.execute("DELETE FROM trash_cleanup WHERE owner=? AND note_id=? AND files_pending=0 "
                            "AND message_ids='[]' AND blocked_ids='[]'", (owner, identity))


class TrashMixin:
    def init_trash(self):
        self.trash = TrashStore(self.store)
        self.trash_confirmations = {}
        self.trash_locks = defaultdict(asyncio.Lock)

    async def trash_callback(self, owner, thread, message_id, data):
        from .bot import button, keyboard
        action, _, token = data.partition(':')
        if action == 'cleartrash':
            notes = self.trash.snapshot(owner, token)
            if not notes:
                await self._show(owner, 'Корзина уже пуста.', thread)
                return True
            confirmation = {'token': new_id(), 'folder': token, 'notes': [n['id'] for n in notes],
                            'expires': time.monotonic() + 600}
            prompt = await self._show(owner, f"Очистить корзину?\nЗаписей: {len(notes)}. "
                'Они и их файлы будут удалены навсегда. Это нельзя отменить.', thread, keyboard([
                    button('Очистить корзину', 'confirmtrash:' + confirmation['token']),
                    button('Отмена', 'canceltrash:' + confirmation['token'])]))
            confirmation['prompt'] = prompt['message_id']
            previous = self.trash_confirmations.get(owner)
            self.trash_confirmations[owner] = confirmation
            if previous:
                await self.remove_menu_message(owner, previous['prompt'])
        elif action in {'confirmtrash', 'canceltrash'}:
            confirmation = self.trash_confirmations.get(owner)
            if (not confirmation or confirmation['token'] != token
                    or confirmation['expires'] <= time.monotonic()
                    or message_id != confirmation['prompt']):
                raise ValueError('Подтверждение устарело. Снова выбери «Очистить корзину»')
            if action == 'canceltrash':
                self.trash_confirmations.pop(owner)
                await self.remove_menu_message(owner, confirmation['prompt'])
                return True
            if self.view_busy(owner):
                raise ValueError('Дождись завершения обработки или экспорта, затем очисти корзину')
            # No await before the transaction: workers cannot claim the target notes.
            count = self.trash.begin(owner, confirmation['folder'], confirmation['notes'])
            self.trash_confirmations.pop(owner)
            await self.resume_trash_cleanup(owner)
            left = self.store.one('SELECT count(*) AS n FROM notes WHERE owner=? AND folder_id=? AND deleted=0',
                                  (owner, confirmation['folder']))['n']
            text = f'Удалено записей: {count}.'
            text = f'Корзина очищена. {text}' if not left else f'{text} В корзине осталось: {left}.'
            jobs = self.trash.jobs(owner)
            if any(row['files_pending'] or json.loads(row['message_ids']) for row in jobs):
                text += '\nОчистка файлов и сообщений Telegram ещё продолжается.'
            if any(json.loads(row['blocked_ids']) for row in jobs):
                text += '\nЧасть сообщений Telegram не разрешил удалить. Их можно убрать вручную.'
            # Edit only our confirmation. A full folder refresh would also remove
            # unrelated chat messages; the lower keyboard must remain untouched.
            await self._show(owner, text, thread, keyboard(), confirmation['prompt'])
        else:
            return False
        return True

    async def resume_trash_cleanup(self, owner):
        async with self.trash_locks[owner]:
            for job in self.trash.jobs(owner):
                identity = job['note_id']
                if job['files_pending']:
                    try:
                        self.archive.delete({'owner': owner, 'id': identity})
                    except OSError:
                        log.warning('trash file cleanup deferred owner=%s note_id=%s', owner, identity)
                    else:
                        with self.store.transaction():
                            self.store.db.execute('UPDATE trash_cleanup SET files_pending=0 WHERE owner=? AND note_id=?',
                                                  (owner, identity))
                pending, blocked = json.loads(job['message_ids']), json.loads(job['blocked_ids'])
                while pending:
                    batch = pending[:100]
                    try:
                        await self.telegram.call('deleteMessages', chat_id=owner, message_ids=batch)
                    except ProviderError as exc:
                        outcome = deletion_error(exc)
                        if outcome not in {DeletionOutcome.FORBIDDEN, DeletionOutcome.MISSING}:
                            break  # Ambiguous outcome: retain every exact ID for retry.
                        completed, forbidden = [], []
                        for mid in batch:
                            result = await delete_message(self.telegram, owner, mid)
                            if result.complete:
                                completed.append(mid)
                            elif result == DeletionOutcome.FORBIDDEN:
                                forbidden.append(mid)
                            else:
                                break
                    else:
                        completed, forbidden = batch, []
                    pending = [mid for mid in pending if mid not in completed and mid not in forbidden]
                    blocked.extend(forbidden)
                    with self.store.transaction():
                        self.store.db.executemany('DELETE FROM telegram_messages WHERE owner=? AND message_id=?',
                                                 [(owner, mid) for mid in completed])
                        self.trash.save(owner, identity, pending, blocked)
                    if len(completed) + len(forbidden) < len(batch):
                        break
                self.trash.save(owner, identity, pending, blocked)

    async def trash_cleanup_worker(self):
        while True:
            for owner in self.config.allowed_user_ids:
                if self.trash.pending(owner):
                    await self.resume_trash_cleanup(owner)
            await asyncio.sleep(5)
