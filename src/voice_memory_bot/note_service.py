"""Archive operations shared by Telegram handlers and durable AI actions."""
from __future__ import annotations

import json
import logging

from .providers import ProviderError

log = logging.getLogger(__name__)


class NoteService:
    def __init__(self, store, messages, archive, telegram, note_locks):
        self.store, self.messages, self.archive = store, messages, archive
        self.telegram, self.note_locks = telegram, note_locks

    def delete_archive(self, owner, identity, *, commit=True):
        note = self.store.note(owner, identity)
        if note["state"] == "processing" or self.note_locks[identity].locked():
            raise ValueError("Дождись завершения обработки, затем удали запись")
        self.store.delete(owner, identity, commit=commit)
        return note

    def delete(self, owner, identity):
        note = self.delete_archive(owner, identity)
        self.archive.delete(note)
        return note

    def move_archive(self, note, folder_id, *, commit=True):
        if note["state"] in {"processing", "queued"}:
            raise ValueError("Дождись завершения обработки, затем перенеси запись")
        if note["file_id"] and not note["audio_bytes"]:
            raise ValueError("Сначала повтори сохранение файла, затем перенеси запись")
        return self.messages.move_archive(note, folder_id, commit=commit)

    async def move(self, owner, identity, folder_id):
        note = self.store.note(owner, identity)
        folder = self.store.folder(owner, folder_id)
        if folder['removed']:
            raise ValueError('Папка удалена. Выбери другую')
        if note["state"] in {"processing", "queued"} or self.note_locks[identity].locked():
            raise ValueError("Дождись завершения обработки, затем перенеси запись")
        async with self.note_locks[identity]:
            if folder_id == note["folder_id"]:
                return "Запись уже в этой папке."
            move = self.move_archive(note, folder_id)
            result = await self.cleanup_move(move)
            return f"Перенесено в «{folder['name']}»." if result == "Запись перенесена." else result

    async def cleanup_move(self, move):
        owner, identity = move["owner"], move["note_id"]
        remaining = {row["message_id"] for row in self.messages.receipts(owner, identity) if not row["deleted"]}
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
            try:
                await self.telegram.call("deleteMessage", chat_id=owner, message_id=target)
            except ProviderError as exc:
                if getattr(exc, "reason", None) != "delete_target_missing":
                    remaining += 1
                    retryable += getattr(exc, "reason", None) != "delete_forbidden"
                    log.warning("note cleanup failed note_id=%s error=%s", identity, exc)
                    continue
            self.messages.removed(owner, identity, target)
        return {"remaining": remaining, "retryable": retryable} if details else remaining
