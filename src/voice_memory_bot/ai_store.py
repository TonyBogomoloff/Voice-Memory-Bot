from __future__ import annotations

import json
import time

from .states import AIState, DeliveryState


class AIStore:
    def __init__(self, store, is_busy=None):
        self.store, self.db = store, store.db
        self.is_busy = is_busy or (lambda identity: False)

    def settings(self, owner):
        self.store.register_user(owner)
        with self.store.transaction():
            self.db.execute("INSERT OR IGNORE INTO ai_settings(owner) VALUES (?)", (owner,))
        row = self.store.one("SELECT * FROM ai_settings WHERE owner=?", (owner,))
        row["names"] = json.loads(row["names"])
        return row

    def set_settings(self, owner, **fields):
        self.settings(owner)
        if not fields or not fields.keys() <= {"wake_enabled", "names", "model", "notes_access"}:
            raise ValueError("Неизвестная настройка ИИ")
        if "names" in fields:
            from .store import normalize
            names = fields["names"]
            if (not isinstance(names, list) or not 1 <= len(names) <= 10
                    or any(not isinstance(n, str) or not 1 <= len(n) <= 32
                           or not all(c.isalnum() or c in " -" for c in n) or not n.strip() for n in names)):
                raise ValueError("Укажи от 1 до 10 имён, до 32 букв в каждом, через запятую")
            fields["names"] = json.dumps(list(dict.fromkeys(normalize(n) for n in names)), ensure_ascii=False)
        if "notes_access" in fields and fields["notes_access"] not in {"none", "read", "write"}:
            raise ValueError("Неизвестный режим доступа")
        if "wake_enabled" in fields and type(fields["wake_enabled"]) is not bool:
            raise ValueError("Некорректный переключатель")
        assignments = ",".join(f"{k}=?" for k in fields)
        with self.store.transaction():
            self.db.execute(f"UPDATE ai_settings SET {assignments} WHERE owner=?", [*fields.values(), owner])

    def mode(self, owner, folder_id, mode):
        if self.store.folder(owner, folder_id)['is_trash']:
            raise ValueError("Режим системной папки «Корзина» менять нельзя")
        if mode not in {"notes", "chat"}:
            raise ValueError("Неизвестный режим папки")
        with self.store.transaction():
            self.db.execute("UPDATE folders SET mode=? WHERE id=? AND owner=?", (mode, folder_id, owner))

    def conversation(self, owner, identity):
        row = self.store.one("SELECT * FROM ai_conversations WHERE id=? AND owner=?", (identity, owner))
        if not row:
            raise ValueError("Разговор не найден")
        return row

    def new_conversation(self, owner, folder_id, active=True):
        from .store import new_id
        self.store.folder(owner, folder_id)
        identity = new_id()
        with self.store.transaction():
            self.db.execute("INSERT INTO ai_conversations VALUES (?,?,?,?)",
                            (identity, owner, folder_id, time.time()))
            if active:
                self.db.execute("INSERT INTO ai_active VALUES (?,?,?) ON CONFLICT(owner,folder_id) "
                                "DO UPDATE SET conversation_id=excluded.conversation_id", (owner, folder_id, identity))
        return identity

    def note_context(self, owner, conversation):
        self.conversation(owner, conversation)
        return self.store.one("SELECT * FROM ai_note_context WHERE owner=? AND conversation_id=?",
                              (owner, conversation))

    def start_note_conversation(self, owner, note_id):
        note = self.store.note(owner, note_id)
        content = note["content"] if note["content"] is not None else note["transcript"]
        if note["kind"] == "photo":
            # Collect the instruction immediately; claim() waits for the download.
            content = content or ""
        elif not content:
            raise ValueError("Дождись текста записи, затем обратись к ИИ")
        if len(content) > 40000:
            raise ValueError("Текст слишком длинный для ИИ. Раздели запись на части до 40000 символов")
        conversation = self.new_conversation(owner, note["folder_id"], active=False)
        with self.store.transaction():
            self.db.execute("INSERT INTO ai_note_context VALUES (?,?,?,?,?)",
                            (conversation, owner, note_id, note["title"] or "Запись", content))
        return conversation

    def active(self, owner, folder_id):
        self.store.folder(owner, folder_id)
        row = self.store.one("SELECT conversation_id FROM ai_active WHERE owner=? AND folder_id=?", (owner, folder_id))
        return row["conversation_id"] if row else self.new_conversation(owner, folder_id)

    def reply_conversation(self, owner, message_id):
        row = self.store.one("SELECT conversation_id FROM ai_links WHERE owner=? AND message_id=?", (owner, message_id))
        if not row:
            note = self.message_note(owner, message_id)
            if note:
                row = self.store.one("SELECT a.conversation_id FROM ai_requests a JOIN notes n ON n.id=a.note_id "
                    "WHERE a.owner=? AND (a.note_id=? OR a.result_note_id=? OR n.reply_message_id=? "
                    "OR n.reply_message_id IN (SELECT message_id FROM note_messages WHERE owner=? AND note_id=?) "
                    "OR EXISTS (SELECT 1 FROM ai_note_context c WHERE c.conversation_id=a.conversation_id "
                    "AND c.owner=a.owner AND c.note_id=?)) ORDER BY a.created_at DESC LIMIT 1",
                    (owner, note['id'], note['id'], note['message_id'], owner, note['id'], note['id']))
        return row["conversation_id"] if row else None

    def message_note(self, owner, message_id):
        if not message_id or message_id < 1:
            return None
        return self.store.one("SELECT n.* FROM notes n WHERE n.owner=? AND n.deleted=0 AND "
            "(n.message_id=? OR EXISTS (SELECT 1 FROM note_messages m WHERE m.owner=n.owner "
            "AND m.note_id=n.id AND m.message_id=?)) LIMIT 1", (owner, message_id, message_id))

    def message_ref(self, owner, identity):
        note = self.store.one("SELECT * FROM notes WHERE owner=? AND id=? AND deleted=0", (owner, identity))
        if not note:
            return None
        messages = self.store.all("SELECT message_id,thread_id FROM note_messages WHERE owner=? "
                                  "AND note_id=? AND deleted=0 ORDER BY message_id", (owner, identity))
        return {"id": identity, "revision": note['revision'], "folder_id": note['folder_id'],
                "kind": note['kind'], "title": note['title'], "telegram_messages": messages,
                "reply_message_id": note['reply_message_id']}

    def conversation_notes(self, owner, conversation, exclude=None):
        """Whole conversation identity list, independent of history text truncation."""
        self.conversation(owner, conversation)
        members = {}
        context = self.note_context(owner, conversation)
        if context:
            members[context['note_id']] = 'source'
        requests = self.store.all("SELECT a.*,n.reply_message_id FROM ai_requests a JOIN notes n ON n.id=a.note_id "
                                   "WHERE a.owner=? AND a.conversation_id=? ORDER BY a.created_at,a.note_id",
                                   (owner, conversation))
        for request in requests:
            source = self.message_note(owner, request['reply_message_id'])
            if source:
                members.setdefault(source['id'], 'source')
            members[request['note_id']] = 'user'
            if request['result_note_id']:
                members[request['result_note_id']] = 'assistant'
            for result in self.store.all("SELECT id FROM notes WHERE owner=? AND ai_generated_from=? AND deleted=0",
                                          (owner, request['note_id'])):
                members[result['id']] = 'assistant'
        return [{**ref, "role": role} for identity, role in members.items() if identity != exclude
                and (ref := self.message_ref(owner, identity))]

    def link(self, owner, message_id, conversation):
        self.conversation(owner, conversation)
        with self.store.transaction():
            self.db.execute("INSERT OR REPLACE INTO ai_links VALUES (?,?,?)", (owner, message_id, conversation))

    def request(self, owner, note_id):
        self.store.note(owner, note_id)
        return self.store.one("SELECT * FROM ai_requests WHERE owner=? AND note_id=?", (owner, note_id))

    def result_request(self, owner, note_id):
        return self.store.one("SELECT a.note_id,a.conversation_id FROM ai_requests a "
                              "JOIN notes n ON n.id=a.note_id WHERE a.owner=? AND a.result_note_id=? "
                              "AND n.deleted=0", (owner, note_id))

    def enqueue(self, owner, note_id, trigger, thread=None, reply_to=None, conversation=None, *, reply_context=None):
        note = self.store.note(owner, note_id)
        existing = self.request(owner, note_id)
        if existing:
            return existing
        pending = self.store.one("SELECT count(*) AS n FROM ai_requests WHERE owner=? "
                                 "AND state IN ('queued','running')", (owner,))["n"]
        if pending >= self.store.limits.max_pending_per_user:
            raise ValueError("Очередь ИИ заполнена; дождись предыдущих ответов")
        folder = self.store.folder(owner, note["folder_id"])
        if conversation:
            self.conversation(owner, conversation)
        else:
            conversation = (self.active(owner, folder["id"]) if folder["mode"] == "chat"
                            else self.new_conversation(owner, folder["id"], active=False))
        with self.store.transaction():
            self.db.execute("INSERT INTO ai_requests(note_id,owner,conversation_id,trigger,thread_id,reply_to,created_at) "
                            "VALUES (?,?,?,?,?,?,?)", (note_id, owner, conversation, trigger, thread, reply_to,
                                                      note["created_at"]))
            self.db.execute("UPDATE ai_requests SET reply_context=? WHERE note_id=?", (reply_context, note_id))
            self.db.execute("UPDATE notes SET entry_type='ai' WHERE id=? AND owner=?", (note_id, owner))
        return self.request(owner, note_id)

    def finish_reply(self, owner, note_id, text):
        request = self.request(owner, note_id)
        with self.store.transaction():
            if not request["result_note_id"] and not self.command_reply(owner, note_id):
                result = result_text(self.store, self.store.note(owner, note_id), text)
                self.db.execute("UPDATE ai_requests SET result_note_id=? WHERE owner=? AND note_id=?",
                                (result["id"], owner, note_id))
            self.db.execute("UPDATE ai_requests SET response=?,state='ready',error=NULL WHERE owner=? AND note_id=?",
                            (text, owner, note_id))

    def command_reply(self, owner, note_id):
        """Operational replies stay in the conversation, not in the notes archive."""
        return any(s["command"]["action"] in {"move_note", "delete_note", "move_notes", "delete_notes"} and s["result"]
                   and (s["result"].get("ok") or s["result"].get("needs_folder_choice")
                        or s["result"].get("cancelled"))
                   for s in self.steps(owner, note_id))

    def archive_replies(self):
        """Expose old successful replies without replaying AI calls or Telegram sends."""
        requests = self.store.all("SELECT a.* FROM ai_requests a JOIN notes n ON n.id=a.note_id "
                                  "WHERE a.state='ready' AND a.result_note_id IS NULL "
                                  "AND a.response IS NOT NULL AND n.deleted=0 ORDER BY a.created_at")
        for request in requests:
            if not request["response"].strip() or self.command_reply(request["owner"], request["note_id"]):
                continue
            with self.store.transaction():
                note = self.store.note(request["owner"], request["note_id"])
                # The historical response time was not recorded; retain the request's date.
                result = result_text(self.store, note, request["response"], created_at=request["created_at"])
                self.db.execute("UPDATE ai_requests SET result_note_id=? WHERE owner=? AND note_id=?",
                                (result["id"], request["owner"], request["note_id"]))

    def claim(self, owner=None):
        row = self.store.one("""SELECT a.* FROM ai_requests a JOIN notes n ON n.id=a.note_id
            WHERE a.state='queued' AND n.state='ready' AND n.deleted=0 AND n.transcript IS NOT NULL
            AND (? IS NULL OR a.owner=?)
            AND NOT EXISTS (SELECT 1 FROM ai_note_context c JOIN notes source ON source.id=c.note_id
                AND source.owner=c.owner WHERE c.conversation_id=a.conversation_id AND c.owner=a.owner
                AND source.kind='photo' AND source.deleted=0 AND source.state IN ('queued','processing'))
            AND NOT EXISTS (SELECT 1 FROM ai_requests b JOIN notes bn ON bn.id=b.note_id
                WHERE b.conversation_id=a.conversation_id AND b.created_at<a.created_at
                AND b.state IN ('queued','running') AND bn.deleted=0 AND bn.state!='asr_failed')
            ORDER BY a.created_at LIMIT 1""", (owner, owner))
        if row:
            with self.store.transaction():
                self.db.execute("UPDATE ai_requests SET state='running' WHERE note_id=?", (row["note_id"],))
        return row

    def update_request(self, owner, note_id, **fields):
        if not self.request(owner, note_id):
            raise ValueError("Запрос не найден")
        if not fields or not fields.keys() <= {"state", "model", "response", "error", "delivery"}:
            raise ValueError("Некорректное поле запроса")
        for name, enum in {'state': AIState, 'delivery': DeliveryState}.items():
            if name in fields:
                fields[name] = enum(fields[name]).value
        assignments = ",".join(f"{k}=?" for k in fields)
        with self.store.transaction():
            self.db.execute(f"UPDATE ai_requests SET {assignments} WHERE note_id=? AND owner=?",
                            [*fields.values(), note_id, owner])

    def history(self, owner, conversation, before=None, limit=12):
        self.conversation(owner, conversation)
        return self.store.all("SELECT a.*,n.transcript FROM ai_requests a JOIN notes n ON n.id=a.note_id "
                              "WHERE a.owner=? AND a.conversation_id=? AND a.state IN ('ready','failed') AND n.deleted=0 "
                              "AND a.created_at<? ORDER BY a.created_at DESC LIMIT ?",
                              (owner, conversation, before or time.time() + 1, limit))[::-1]

    def steps(self, owner, note_id):
        if not self.request(owner, note_id):
            raise ValueError("Запрос не найден")
        return [{**row, "command": json.loads(row["command"]),
                 "result": json.loads(row["result"]) if row["result"] is not None else None}
                for row in self.store.all("SELECT * FROM ai_steps WHERE note_id=? ORDER BY step", (note_id,))]

    def receipts(self, owner, request_id):
        return [item for step in self.steps(owner, request_id) if step['result'] and step['result'].get('ok')
                for item in step['result'].get('items', [step['result']])]

    def history_receipts(self, owner, conversation, before):
        self.conversation(owner, conversation)
        rows = self.store.all("SELECT s.note_id,s.result FROM ai_steps s JOIN ai_requests a ON a.note_id=s.note_id "
                              "WHERE a.owner=? AND a.conversation_id=? AND a.created_at<? AND s.result IS NOT NULL "
                              "ORDER BY a.created_at,s.step", (owner, conversation, before))
        receipts = {}
        for row in rows:
            result = json.loads(row['result'])
            if result.get('ok'):
                receipts.setdefault(row['note_id'], []).extend(result.get('items', [result]))
        return receipts

    def save_command(self, owner, note_id, step, command):
        if not self.request(owner, note_id):
            raise ValueError("Запрос не найден")
        with self.store.transaction():
            self.db.execute("INSERT OR IGNORE INTO ai_steps(note_id,step,command) VALUES (?,?,?)",
                            (note_id, step, json.dumps(command, ensure_ascii=False)))

    def execute(self, owner, note_id, step):
        """Commit the operation and its receipt in one SQLite transaction, with no awaits."""
        saved = next(s for s in self.steps(owner, note_id) if s["step"] == step)
        if saved["result"] is not None:
            return saved["result"]
        command = saved["command"]
        access = self.settings(owner)["notes_access"]
        with self.store.transaction():
            try:
                if access == "none" or (command["action"] in {"create_note", "update_note"} and access != "write"):
                    raise ValueError("Доступ к заметкам выключен в настройках ИИ")
                context = self.note_context(owner, self.request(owner, note_id)["conversation_id"])
                allowed = {n['id'] for n in self.conversation_notes(owner, self.request(owner, note_id)['conversation_id'])}
                folder_reads = {n['id'] for s in self.steps(owner, note_id)
                                if s['command']['action'] == 'read_folder' and s['result']
                                for n in s['result'].get('notes', [])}
                if command['action'] == 'read_note':
                    allowed.update(folder_reads)
                if context and command["action"] not in {"list_folders", "list_conversation", "read_folder"} and (command["action"] not in {"read_note", "update_note", "get_message_folder"}
                                or command["note_id"] not in allowed):
                    raise ValueError("Этот диалог относится только к выбранной записи")
                result = self._operation(owner, note_id, command)
            except ValueError as exc:
                result = {"error": str(exc)}
            self.db.execute("UPDATE ai_steps SET result=? WHERE note_id=? AND step=?",
                            (json.dumps(result, ensure_ascii=False), note_id, step))
        return result

    def _operation(self, owner, request_id, command):
        from .store import clean_name, new_id
        action = command["action"]
        if action == "list_conversation":
            request = self.request(owner, request_id)
            return {"notes": self.conversation_notes(owner, request['conversation_id'], exclude=request_id),
                    "current_command_id": request_id, "complete": True}
        if action == "get_message_folder":
            note = self.store.note(owner, command["note_id"])
            folder = self.store.folder(owner, note["folder_id"])
            return {"note_id": note["id"],
                    "folder": {k: folder[k] for k in ("id", "name", "mode")}}
        if action == "list_folders":
            return {"folders": [{"id": f["id"], "name": f["name"], "aliases": json.loads(f["aliases"]),
                                  "mode": f["mode"]} for f in self.store.folders(owner)]}
        folder_id = command["folder_id"] or None
        if folder_id:
            self.store.folder(owner, folder_id)
        if action == "read_folder":
            if not folder_id:
                raise ValueError("Укажи папку для чтения")
            folder = self.store.folder(owner, folder_id)
            notes = self.store.all(
                "SELECT * FROM notes WHERE owner=? AND folder_id=? AND deleted=0 AND id!=? "
                "ORDER BY created_at,id", (owner, folder_id, request_id))
            # Some command replies live only in ai_requests, without a standalone note.
            replies = {r['note_id']: r['response'] for r in self.store.all(
                "SELECT a.note_id,a.response FROM ai_requests a JOIN notes n ON n.id=a.note_id "
                "WHERE a.owner=? AND n.owner=? AND n.folder_id=? AND n.deleted=0 "
                "AND a.state='ready' AND a.result_note_id IS NULL AND a.response IS NOT NULL",
                (owner, owner, folder_id))}
            return {"folder": {k: folder[k] for k in ("id", "name", "mode")},
                    "notes": [{**{k: n[k] for k in ("id", "title", "kind", "entry_type", "state",
                                                   "created_at", "revision", "folder_id")},
                               "role": "assistant" if n["ai_generated_from"] else "user",
                               "assistant_response": replies.get(n['id']),
                               "content": n["content"] if n["content"] is not None else n["transcript"] or ""}
                              for n in notes], "count": len(notes), "complete": True}
        if action == "search_notes":
            matches = self.store.notes(owner, folder_id, query=command["query"] or None, limit=None)
            return {"notes": [{"id": n["id"], "title": n["title"], "folder_id": n["folder_id"], "kind": n["kind"],
                               "created_at": n["created_at"], "revision": n['revision'],
                               "excerpt": (n["content"] if n["content"] is not None else n["transcript"] or "")[:250]}
                              for n in matches], "has_more": False}
        if action == "read_note":
            note = self.store.note(owner, command["note_id"])
            content = note["content"] if note["content"] is not None else note["transcript"] or ""
            return {"id": note["id"], "title": note["title"], "folder_id": note["folder_id"], "kind": note["kind"],
                    "revision": note["revision"], "content": content[:40000], "truncated": len(content) > 40000}
        if action not in {"create_note", "update_note"}:
            raise ValueError("Неизвестная операция")
        title = clean_name(command["title"])
        content = command["content"]
        if not isinstance(content, str) or not content.strip() or len(content) > 40000:
            raise ValueError("Текст заметки должен содержать от 1 до 40000 символов")
        if action == "create_note":
            if not folder_id:
                raise ValueError("Выбери папку из списка")
            count = self.store.one("SELECT count(*) AS n FROM notes WHERE owner=? AND deleted=0", (owner,))["n"]
            if count >= self.store.limits.max_notes_per_user:
                raise ValueError("Достигнут лимит заметок")
            identity = new_id()
            # Incoming Telegram message IDs are positive. Generated notes use a separate negative sequence.
            message = self.store.one("SELECT min(0,coalesce(min(message_id),0))-1 AS id FROM notes WHERE owner=?",
                                     (owner,))["id"]
            self.db.execute("""INSERT INTO notes(id,owner,folder_id,source_folder,message_id,kind,extension,
                created_at,state,transcript,title,routing,delivery,transcript_delivery,archive_dirty)
                VALUES (?,?,?,?,?,'text','txt',?,'ready',?,?,'manual','sent','sent',1)""",
                (identity, owner, folder_id, folder_id, message, time.time(), content, title))
            return {"ok": True, "action": action, "id": identity, "title": title, "revision": 0}
        note = self.store.note(owner, command["note_id"])
        if folder_id and folder_id != note["folder_id"]:
            raise ValueError("Для переноса используй move_note. update_note меняет только текст")
        if self.is_busy(note["id"]):
            raise ValueError("Запись сейчас обрабатывается или переносится; попробуй после завершения")
        if note["state"] != "ready" or note["id"] == request_id:
            raise ValueError("Можно менять только готовые заметки")
        read = [s["result"] for s in self.steps(owner, request_id)
                if s["command"]["action"] == "read_note" and s["result"]
                and s["result"].get("id") == note["id"] and not s["result"].get("truncated", True)]
        if not read or command["revision"] != note["revision"] or read[-1]["revision"] != note["revision"]:
            raise ValueError("Заметка изменилась или ещё не прочитана; сначала выполни read_note")
        folder_id = folder_id or note["folder_id"]
        self._snapshot(note, request_id)
        self.db.execute("UPDATE notes SET title=?,content=?,folder_id=?,revision=revision+1,archive_dirty=1 "
                        "WHERE id=? AND owner=?", (title, content, folder_id, note["id"], owner))
        return {"ok": True, "action": action, "id": note["id"], "title": title, "revision": note["revision"] + 1}

    def _snapshot(self, note, request_id=None):
        self.db.execute("INSERT INTO note_revisions VALUES (?,?,?,?,?,?,?)",
                        (note["id"], note["revision"], note["title"], note["content"], note["folder_id"],
                         request_id, time.time()))

    def undo(self, owner, note_id, revision):
        note = self.store.note(owner, note_id)
        if note["revision"] != revision or revision < 1 or note["state"] != "ready":
            raise ValueError("После этой правки запись уже менялась. Открой актуальную заметку")
        old = self.store.one("SELECT * FROM note_revisions WHERE note_id=? AND revision=?", (note_id, revision - 1))
        if not old:
            raise ValueError("Предыдущая версия не найдена")
        with self.store.transaction():
            self._snapshot(note)
            self.db.execute("UPDATE notes SET title=?,content=?,folder_id=?,revision=revision+1,archive_dirty=1 "
                            "WHERE id=? AND owner=?", (old["title"], old["content"], old["folder_id"], note_id, owner))
        return self.store.note(owner, note_id)


def result_text(store, source, text, *, created_at=None):
    """Archive an accepted AI result inside the caller's transaction.

    Intake quotas must not discard a reply after its request has been accepted.
    This also preserves complete historical replies without the intake length limit.
    """
    from .store import new_id
    store.note(source["owner"], source["id"])
    identity = new_id()
    message = store.one("SELECT min(0,coalesce(min(message_id),0))-1 AS id FROM notes WHERE owner=?",
                        (source["owner"],))["id"]
    store.db.execute("""INSERT INTO notes(id,owner,folder_id,source_folder,message_id,source_thread,
        kind,extension,created_at,state,transcript,title,origin,ai_generated_from,routing,
        delivery,transcript_delivery,archive_dirty)
        VALUES (?,?,?,?,?,?,'text','txt',?,'ready',?,'Ответ ИИ',?,?,'manual','sent','sent',1)""",
        (identity, source["owner"], source["folder_id"], source["folder_id"], message, source["source_thread"],
         time.time() if created_at is None else created_at, text,
         "ai", source["id"]))
    return store.note(source["owner"], identity)
