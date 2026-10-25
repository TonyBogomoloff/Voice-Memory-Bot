"""Batch-capable AI actions with durable receipts and explicit folder choices."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from difflib import SequenceMatcher

from .store import clean_name, normalize

NOTE_ACTIONS = {"move_note", "delete_note"}
BATCH_ACTIONS = {"move_notes": "move_note", "delete_notes": "delete_note"}


def folder_names(folder):
    return [folder["name"], *json.loads(folder["aliases"])]


def matching_folder(folders, name):
    return next((f for f in folders if normalize(name) in map(normalize, folder_names(f))), None)


def suggested_folders(folders, name, source):
    def score(folder):
        return max(SequenceMatcher(None, normalize(name), normalize(n)).ratio() for n in folder_names(folder))
    return [{"id": f["id"], "name": f["name"]}
            for f in sorted((f for f in folders if f["id"] != source), key=score, reverse=True)[:4]]


class NoteActions:
    def __init__(self, bot):
        self.bot, self.store, self.state = bot, bot.store, bot.ai_state
        self.locks = defaultdict(asyncio.Lock)

    def saved(self, owner, request, index):
        return next((s for s in self.state.steps(owner, request) if s["step"] == index), None)

    def save_result(self, request, index, result, identity=None):
        if identity:
            saved = self.store.one("SELECT command,result FROM ai_steps WHERE note_id=? AND step=?", (request, index))
            if json.loads(saved['command'])['action'] in BATCH_ACTIONS:
                aggregate = json.loads(saved['result']) if saved['result'] else {'ok': True, 'items': []}
                aggregate['items'] = [r for r in aggregate['items'] if r['id'] != identity] + [result]
                result = aggregate
        self.store.db.execute("UPDATE ai_steps SET result=? WHERE note_id=? AND step=?",
                              (json.dumps(result, ensure_ascii=False) if result is not None else None, request, index))

    def validate(self, owner, request, index, command, access, versions, allowed):
        if access != "write":
            raise ValueError("Для переноса и удаления включи запись в настройках ИИ")
        action, identity = command["action"], command["note_id"]
        if action not in NOTE_ACTIONS or identity == request:
            raise ValueError("Текущую выполняемую команду нельзя перенести или удалить")
        if allowed is not None and identity not in allowed:
            raise ValueError("Этот диалог относится только к выбранной записи и её обсуждению")
        note = self.store.note(owner, identity)
        if note["state"] != "ready" or self.state.is_busy(identity):
            raise ValueError("Дождись завершения обработки записи")
        read = versions.get(identity)
        if not read or command['revision'] != note['revision'] or read['revision'] != note['revision']:
            raise ValueError("Запись изменилась или ещё не прочитана. Прочитай её заново через read_note")
        if action == "move_note" and note["file_id"] and not note["audio_bytes"]:
            raise ValueError("Сначала дождись сохранения исходного файла")
        return note

    def read_versions(self, steps, index):
        versions = {}
        for step in steps:
            if step['step'] >= index or not step['result']:
                continue
            if step['command']['action'] == 'read_note' and 'id' in step['result']:
                versions[step['result']['id']] = step['result']
            elif step['command']['action'] in {'list_conversation', 'search_notes'}:
                versions.update({n['id']: n for n in step['result'].get('notes', [])})
        return versions

    def validate_many(self, owner, request, index, command, access, revisions=None):
        action = BATCH_ACTIONS.get(command['action'], command['action'])
        ids = command.get('note_ids', []) if command['action'] in BATCH_ACTIONS else [command['note_id']]
        if not ids:
            raise ValueError('Выбери сообщения для переноса или удаления')
        versions = self.read_versions(self.state.steps(owner, request), index)
        if command['action'] in BATCH_ACTIONS and any(identity not in versions for identity in ids):
            raise ValueError('Для пакетного переноса/удаления сначала вызови list_conversation (всё обсуждение) '
                             'или search_notes (другие заметки). Они возвращают все ID и ревизии за один шаг; '
                             'не читай каждое сообщение отдельно через read_note.')
        conversation = self.state.request(owner, request)['conversation_id']
        allowed = ({n['id'] for n in self.state.conversation_notes(owner, conversation)}
                   if self.state.note_context(owner, conversation) else None)
        notes = []
        for identity in dict.fromkeys(ids):
            revision = (versions.get(identity, {}).get('revision', -1) if command['action'] in BATCH_ACTIONS
                        else command['revision'])
            if revisions is not None:
                revision = revisions.get(identity, -1)
            single = {**command, 'action': action, 'note_id': identity, 'revision': revision}
            notes.append((single, self.validate(owner, request, index, single, access, versions, allowed)))
        return notes

    def apply(self, owner, request, index, command, note, folder=None):
        action, identity = command["action"], note["id"]
        result = {"ok": True, "action": action, "id": identity, "title": note["title"] or "Запись"}
        if action == "move_note":
            if folder["removed"]:
                raise ValueError("Папка удалена. Выбери другую")
            result.update(folder_id=folder["id"], folder_name=folder["name"],
                          message=f"Перенесено в «{folder['name']}».")
            if note['entry_type'] == 'ai':
                self.store.db.execute("UPDATE notes SET entry_type='note',archive_dirty=1 WHERE owner=? AND id=?",
                                      (owner, identity))
            if folder["id"] == note["folder_id"]:
                result["message"] = "Запись уже в этой папке."
                self.save_result(request, index, result, identity)
                return result
            self.bot.notes.move_archive(note, folder["id"], commit=False)
        else:
            self.bot.notes.delete_archive(owner, identity, commit=False)
            result["message"] = "Запись удалена из архива. Завершаю удаление файлов и сообщений…"
        self.store.db.execute("INSERT INTO ai_message_jobs VALUES (?,?,?,?,?,'pending',?)",
                              (request, index, owner, identity, action, time.time()))
        self.save_result(request, index, result, identity)
        return result

    async def execute(self, owner, request, index):
        async with self.locks[owner]:
            saved = self.saved(owner, request, index)
            if saved["result"] is None:
                command = saved["command"]
                access = self.state.settings(owner)["notes_access"]
                try:
                    with self.store.db:
                        self.store.db.execute("BEGIN IMMEDIATE")
                        notes = self.validate_many(owner, request, index, command, access)
                        folder = None
                        if command["action"] in {"move_note", "move_notes"}:
                            folders = self.store.folders(owner)
                            folder = (self.store.folder(owner, command["folder_id"]) if command["folder_id"]
                                      else matching_folder(folders, command["title"]))
                            if folder is None:
                                name = clean_name(command["title"])
                                result = {"needs_folder_choice": True, "name": name,
                                          "folders": suggested_folders(folders, name, notes[0][1]["folder_id"]),
                                          "revisions": {n["id"]: n["revision"] for _, n in notes},
                                          "message": f"Папку «{name}» не нашёл. Хочешь создать новую папку "
                                                     "или перенести сообщения в одну из существующих?"}
                                self.save_result(request, index, result)
                                return result
                        for single, note in notes:
                            self.apply(owner, request, index, single, note, folder)
                except ValueError as exc:
                    with self.store.db:
                        self.save_result(request, index, {"error": str(exc)})
            await self.cleanup(request, index)
            return self.saved(owner, request, index)["result"]

    def choices(self, owner, request):
        return [{"step": s["step"], **s["result"]} for s in self.state.steps(owner, request)
                if s["result"] and s["result"].get("needs_folder_choice")]

    def buttons(self, owner, request):
        rows = []
        for choice in self.choices(owner, request):
            prefix = f"aiplace:{request}:{choice['step']}:"
            rows.append([{"text": "➕ Новая папка", "callback_data": prefix + "new"}])
            rows.extend([[{"text": "📁 " + f["name"], "callback_data": prefix + f["id"]}]
                         for f in choice["folders"]])
            rows.append([{"text": "Отмена", "callback_data": prefix + "cancel"}])
        return rows

    async def choose(self, owner, request, index, target):
        async with self.locks[owner]:
            saved = self.saved(owner, request, index)
            if not saved or saved["command"]["action"] not in {"move_note", "move_notes"}:
                raise ValueError("Предложение не найдено")
            pending = saved["result"] or {}
            if not pending.get("needs_folder_choice"):
                # Replayed taps never create a folder or move a note again.
                return pending.get("message", "Этот выбор уже закрыт")
            access = self.state.settings(owner)["notes_access"]
            with self.store.db:
                self.store.db.execute("BEGIN IMMEDIATE")
                if target == "cancel":
                    result = {"cancelled": True, "message": "Перенос отменён."}
                    self.save_result(request, index, result)
                else:
                    notes = self.validate_many(owner, request, index, saved["command"], access, pending.get("revisions"))
                    if target == "new":
                        folder = matching_folder(self.store.folders(owner), pending["name"])
                        if folder is None:
                            folder = self.store.create_folder(owner, pending["name"], commit=False)
                    elif target in {f["id"] for f in pending["folders"]}:
                        folder = self.store.folder(owner, target)
                    else:
                        raise ValueError("Выбери папку из предложенных кнопок")
                    self.save_result(request, index, None)
                    for single, note in notes:
                        self.apply(owner, request, index, single, note, folder)
                    result = self.saved(owner, request, index)['result']
                    if saved['command']['action'] in BATCH_ACTIONS:
                        result['message'] = f"Перенесено сообщений: {len(notes)}."
                        self.save_result(request, index, result)
                self.finish_choice(owner, request, result.get("message", "Перенос выполнен."))
            await self.cleanup(request, index)
            result = self.saved(owner, request, index)["result"]
            with self.store.db:
                self.finish_choice(owner, request, result.get("message", "Перенос выполнен."))
            self.bot.flush_ai_archive()
            return result.get("message", "Перенос выполнен.")

    def finish_choice(self, owner, request, message):
        self.store.db.execute("UPDATE ai_requests SET response=? WHERE owner=? AND note_id=?", (message, owner, request))
        self.store.db.execute("UPDATE notes SET content=?,archive_dirty=1,revision=revision+1 WHERE owner=? "
                              "AND deleted=0 AND id=(SELECT result_note_id FROM ai_requests WHERE note_id=?)",
                              (message, owner, request))

    async def cleanup(self, request, index, identity=None):
        if identity is None:
            for job in self.store.all("SELECT note_id FROM ai_message_jobs WHERE request_id=? AND step=? AND state='pending'",
                                      (request, index)):
                await self.cleanup(request, index, job['note_id'])
            return
        job = self.store.one("SELECT * FROM ai_message_jobs WHERE request_id=? AND step=? AND note_id=? AND state='pending'",
                              (request, index, identity))
        if not job or self.bot.note_locks[job["note_id"]].locked():
            return
        async with self.bot.note_locks[job["note_id"]]:
            owner, identity = job["owner"], job["note_id"]
            note = self.store.one("SELECT * FROM notes WHERE owner=? AND id=?", (owner, identity))
            state = "done"
            try:
                if job["action"] == "delete_note":
                    self.bot.archive.delete(note)
                    details = await self.bot.delete_note_messages(note, details=True)
                    state = "pending" if details["retryable"] else "blocked" if details["remaining"] else "done"
                    message = ("Запись удалена из архива и чата." if state == "done" else
                               "Запись удалена из архива, но часть сообщений пока не удалось удалить из Telegram.")
                else:
                    if not note["deleted"]:
                        self.bot.archive.persist_text(note, self.store.folder(owner, note["folder_id"]))
                        self.store.update_note(owner, identity, archive_dirty=0)
                        move = self.bot.messages.get(owner, identity)
                        message = await self.bot.cleanup_move(move)
                        state = self.bot.messages.get(owner, identity)["state"]
                        state = "pending" if state == "cleanup" else state
                    else:
                        message = "Запись была перенесена, затем удалена."
            except OSError:
                state, message = "pending", "Изменение сохранено. Завершаю обновление файлов архива…"
            with self.store.db:
                self.store.db.execute("UPDATE ai_message_jobs SET state=?,updated_at=? WHERE request_id=? AND step=? AND note_id=?",
                                      (state, time.time(), request, index, identity))
                saved = self.store.one("SELECT result FROM ai_steps WHERE note_id=? AND step=?", (request, index))
                if saved:
                    result = json.loads(saved['result'])
                    if 'items' in result:
                        result = next(r for r in result['items'] if r['id'] == identity)
                    if job["action"] == "move_note" and message == "Запись перенесена.":
                        message = f"Перенесено в «{result['folder_name']}»."
                    result.update(message=message, cleanup_state=state)
                    self.save_result(request, index, result, identity)

    async def resume(self):
        job = self.store.one("SELECT * FROM ai_message_jobs WHERE state='pending' AND updated_at<? "
                              "ORDER BY updated_at LIMIT 1", (time.time() - 3,))
        if job and not self.locks[job["owner"]].locked():
            async with self.locks[job["owner"]]:
                await self.cleanup(job["request_id"], job["step"], job["note_id"])
