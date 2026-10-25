"""Assemble authorized full context without executing assistant actions."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ContextSnapshot:
    payload: dict
    images: tuple[Path, ...]
    utf8_bytes: int
    history_complete: bool


class ContextBuilder:
    def __init__(self, state, archive=None):
        self.state, self.archive = state, archive

    def build(self, request, steps, *, image_generation=False):
        payload = self.payload(request, steps, image_generation=image_generation)
        return ContextSnapshot(payload, tuple(self.images(request, steps)),
                               len(json.dumps(payload, ensure_ascii=False).encode()),
                               not payload['history_truncated'])

    def payload(self, request, steps, *, image_generation=False):
        owner = request["owner"]
        note = self.state.store.note(owner, request["note_id"])
        text = request.get("input_text") if request.get("input_text") is not None else note["transcript"]
        if len(text or "") > 40000:
            raise ValueError("Запрос слишком длинный для ИИ. Раздели его на части до 40000 символов")
        previous = self.state.history(owner, request["conversation_id"], before=request["created_at"], limit=-1)
        receipts = self.state.history_receipts(owner, request['conversation_id'], request['created_at'])
        history = [{"user": item["input_text"] if item.get("input_text") is not None else item["transcript"],
                    "reply_context": item.get("reply_context"),
                    "user_note_id": item['note_id'], "assistant_note_id": item['result_note_id'] or item['note_id'],
                    "user_message": self.state.message_ref(owner, item['note_id']),
                    "assistant_message": self.state.message_ref(owner, item['result_note_id'] or item['note_id']),
                    "assistant": item["response"], "receipts": receipts.get(item["note_id"], [])}
                   for item in previous]
        context = self.state.note_context(owner, request["conversation_id"])
        selected_note = (self.state.store.one("SELECT * FROM notes WHERE owner=? AND id=? AND deleted=0",
                                             (owner, context["note_id"])) if context else None)
        reply_note = self.state.message_note(owner, note['reply_message_id'])
        access = self.state.settings(owner)["notes_access"]
        return {"request": text, "reply_context": request.get("reply_context"), "request_note_id": note["id"], "history": history,
                "request_message_id": note['message_id'] if note['message_id'] > 0 else None,
                "reply_message_id": note['reply_message_id'], "conversation_id": request['conversation_id'],
                "conversation_messages": self.state.conversation_notes(owner, request['conversation_id'], exclude=note['id'])
                                         if access != 'none' else [],
                "selected_note": ({"id": context["note_id"], "title": context["title"],
                                   "content": context["content"] if selected_note else "",
                                   "kind": selected_note["kind"] if selected_note else "deleted"} if context else None),
                "reply_note_id": reply_note["id"] if reply_note and access != "none" else None,
                "image_generation": image_generation,
                "history_truncated": len(history) < len(previous), "notes_access": access,
                "current_folder_id": note["folder_id"],
                "folders": [{"id": f["id"], "name": f["name"], "mode": f["mode"]}
                            for f in self.state.store.folders(owner)] if access != "none" else [],
                "steps": [{"command": s["command"], "result": s["result"]} for s in steps]}

    def images(self, request, steps):
        if not self.archive:
            return []
        images = []
        current = self.state.store.note(request["owner"], request["note_id"])
        if current["kind"] == "photo":
            path = self.archive.audio(current)
            if not current["audio_bytes"] or not path.is_file():
                raise ValueError("Фото недоступно. Пришли его заново.")
            images.append(path)
        context = self.state.note_context(request["owner"], request["conversation_id"])
        if context:
            # Selecting this photo authorizes its analysis independently of archive access.
            note = self.state.store.one("SELECT * FROM notes WHERE owner=? AND id=? AND deleted=0",
                                        (request["owner"], context["note_id"]))
            if note and note["kind"] == "photo":
                path = self.archive.audio(note)
                if note["state"] != "ready" or not note["audio_bytes"] or not path.is_file():
                    raise ValueError("Фото недоступно. Пришли его заново и выбери «ИИ».")
                images.append(path)
        if self.state.settings(request["owner"])["notes_access"] == "none":
            return images
        for step in reversed(steps):
            result = step["result"]
            if step["command"]["action"] != "read_note" or not result or result.get("kind") != "photo":
                continue
            # Never take file paths or owners from model output.
            note = self.state.store.one("SELECT * FROM notes WHERE owner=? AND id=? AND deleted=0",
                                        (request["owner"], result["id"]))
            if not note:
                continue
            path = self.archive.audio(note)
            if note["audio_bytes"] and path.is_file() and path not in images:
                images.append(path)
            if len(images) == 3:
                break
        return images[::-1]

