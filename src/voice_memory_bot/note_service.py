"""Archive operations shared by Telegram handlers and durable AI actions."""
from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack

from .delivery import DeletionOutcome, delete_message
from .note_branches import deletion_branch, note_branch

log = logging.getLogger(__name__)


class NoteService:
    def __init__(self, store, messages, archive, telegram, note_locks):
        self.store, self.messages, self.archive = store, messages, archive
        self.telegram, self.note_locks = telegram, note_locks

    def delete_archive(self, owner, identity):
        note = self.store.note(owner, identity)
        self.validate_deletion(note)
        self.store.delete(owner, identity)
        return note

    def delete(self, owner, identity):
        # Resolve and validate the whole scope before changing any record or file.
        # This synchronous transaction cannot interleave with an async worker.
        with self.store.transaction():
            branch = self.deletion_branch(owner, [identity])
            for note in branch.notes:
                self.validate_deletion(note)
            for note in branch.notes:
                self.delete_archive(owner, note['id'])
            branch.detach(self.store, owner)
        for note in branch.notes:
            self.archive.delete(note)
        return branch.notes

    def deletion_branch(self, owner, identities, *, exclude=None):
        return deletion_branch(self.store, owner, identities, exclude=exclude)

    def validate_deletion(self, note):
        if (note['state'] == 'processing' or self.note_locks[note['id']].locked()
                or note['delivery'] == 'sending' or note['transcript_delivery'] == 'sending'
                or self.store.one("SELECT 1 FROM ai_requests WHERE owner=? AND (note_id=? OR result_note_id=?) "
                                  "AND (state IN ('queued','running') OR delivery='sending')",
                                  (note['owner'], note['id'], note['id']))):
            raise ValueError("Дождись завершения обработки всей ветки, затем удали запись")

    def move_archive(self, note, folder_id):
        if note["state"] in {"processing", "queued"}:
            raise ValueError("Дождись завершения обработки, затем перенеси запись")
        if note["file_id"] and not note["audio_bytes"]:
            raise ValueError("Сначала повтори сохранение файла, затем перенеси запись")
        return self.messages.move_archive(note, folder_id)

    def branch(self, owner, identities, *, exclude=None):
        return note_branch(self.store, owner, identities, exclude=exclude)

    def validate_branch_move(self, note):
        if (note["state"] in {"processing", "queued"} or self.note_locks[note['id']].locked()
                or self.store.one("SELECT 1 FROM ai_requests WHERE owner=? AND note_id=? "
                                  "AND state IN ('queued','running')", (note['owner'], note['id']))):
            raise ValueError("Дождись завершения обработки всей ветки, затем перенеси запись")
        if note["file_id"] and not note["audio_bytes"]:
            raise ValueError("Сначала повтори сохранение файла, затем перенеси запись")

    async def move(self, owner, identity, folder_id):
        folder = self.store.folder(owner, folder_id)
        if folder['removed']:
            raise ValueError('Папка удалена. Выбери другую')
        branch = self.branch(owner, [identity])
        for note in branch.notes:
            self.validate_branch_move(note)
        async with AsyncExitStack() as locks:
            for note in sorted(branch.notes, key=lambda n: n['id']):
                await locks.enter_async_context(self.note_locks[note['id']])
            # No awaits until all destinations and cleanup receipts commit together.
            with self.store.transaction():
                moves = [self.move_archive(note, folder_id) for note in branch.notes
                         if note['folder_id'] != folder_id]
                branch.relocate(self.store, owner, folder_id)
            if not moves:
                return "Ветка уже в этой папке." if len(branch.notes) > 1 else "Запись уже в этой папке."
            for move in moves:
                await self.cleanup_move(move)
            if len(branch.notes) == 1:
                state = self.messages.get(owner, identity)['state']
                if state == 'cleanup':
                    return "Запись перенесена. Удаляю исходные сообщения…"
                if state == 'blocked':
                    return "Запись перенесена, но Telegram не разрешил удалить часть исходных сообщений."
                return f"Перенесено в «{folder['name']}»."
            result = f"Ветка перенесена в «{folder['name']}». Записей: {len(branch.notes)}."
            states = {self.messages.get(owner, m['note_id'])['state'] for m in moves}
            if 'blocked' in states:
                result += " Telegram не разрешил удалить часть исходных сообщений."
            if 'cleanup' in states:
                result += " Удаляю исходные сообщения…"
            return result

    async def cleanup_move(self, move):
        owner, identity = move["owner"], move["note_id"]
        remaining = {row["message_id"] for row in self.messages.receipts(owner, identity) if not row["deleted"]}
        transient, forbidden = False, False
        for message_id in json.loads(move["source_ids"]):
            if message_id not in remaining:
                continue
            result = await self.remove_message(owner, identity, message_id)
            forbidden |= result == DeletionOutcome.FORBIDDEN
            transient |= result == DeletionOutcome.RETRY
        if transient:
            self.messages.state(owner, identity, "cleanup")
            return "Запись перенесена. Удаляю исходные сообщения…"
        self.messages.state(owner, identity, "blocked" if forbidden else "done")
        return ("Запись перенесена, но Telegram не разрешил удалить часть исходных сообщений."
                if forbidden else "Запись перенесена.")

    async def remove_message(self, owner, identity, message_id):
        result = await delete_message(self.telegram, owner, message_id)
        if result.complete:
            self.messages.removed(owner, identity, message_id)
        return result

    async def delete_note_messages(self, note, *, details=False):
        owner, identity = note["owner"], note["id"]
        receipts = self.messages.receipts(owner, identity)
        removed = {row["message_id"] for row in receipts if row["deleted"]}
        targets = {row["message_id"] for row in receipts if not row["deleted"]}
        # Legacy records retain only these exact IDs. Never infer nearby messages.
        targets.update(note[field] for field in (
            "message_id", "status_message_id", "transcript_message_id", "result_message_id")
            if note.get(field) and note[field] > 0 and note[field] not in removed)
        remaining = retryable = 0
        for target in sorted(targets):
            result = await self.remove_message(owner, identity, target)
            if not result.complete:
                remaining += 1
                retryable += result == DeletionOutcome.RETRY
                log.warning("note cleanup deferred note_id=%s outcome=%s", identity, result)
        return {"remaining": remaining, "retryable": retryable} if details else remaining
