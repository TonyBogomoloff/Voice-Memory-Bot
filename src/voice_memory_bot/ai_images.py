"""Owner-scoped image actions with a durable artifact and atomic archive receipt."""

from __future__ import annotations

import hashlib
import json
import shutil

from .image_generation import durable_bytes, png_bytes
from .store import clean_name


def authorize_image(engine, request, command):
    state, owner = engine.state, request["owner"]
    store = state.store
    if (request["trigger"] != "miniapp" or not engine.archive
            or not callable(getattr(engine.provider, "generate_image", None))):
        raise ValueError("Генерация картинок доступна через «Выполнить» в мини-эпе")
    if state.settings(owner)["notes_access"] != "write":
        raise ValueError("Для сохранения картинки включи запись в настройках доступа ИИ к заметкам")
    current = store.note(owner, request["note_id"])
    folder = store.folder(owner, command["folder_id"] or current["folder_id"])
    if folder["mode"] == "chat" and not command["folder_id"]:
        folder = store.folder(owner, store.current_folder(owner))
    if not command["content"].strip() or len(command["content"]) > 8000:
        raise ValueError("Описание картинки должно содержать от 1 до 8000 символов")
    source = None
    if command["action"] == "edit_image":
        note = store.note(owner, command["note_id"])
        reads = [s["result"] for s in state.steps(owner, request["note_id"])
                 if s["command"]["action"] == "read_note" and s["result"]
                 and s["result"].get("id") == note["id"]]
        if (note["kind"] != "photo" or note["state"] != "ready" or not reads
                or reads[-1].get("revision") != note["revision"]):
            raise ValueError("Сначала прочитай актуальную запись с изображением через read_note")
        source = engine.archive.audio(note)
        if not note["audio_bytes"] or not source.is_file() or source.stat().st_size != note["audio_bytes"]:
            raise ValueError("Исходная картинка недоступна")
    count = store.one("SELECT count(*) AS n,coalesce(sum(max(audio_bytes,declared_bytes)),0) AS bytes "
                      "FROM notes WHERE owner=? AND deleted=0", (owner,))
    if count["n"] >= store.limits.max_notes_per_user or count["bytes"] >= store.limits.max_user_storage_bytes:
        raise ValueError("Достигнут лимит хранения заметок")
    return folder["id"], source


async def execute_image(engine, request, index):
    state, owner, identity = engine.state, request["owner"], request["note_id"]
    saved = next(s for s in state.steps(owner, identity) if s["step"] == index)
    job = engine.archive.directory(state.store.note(owner, identity)) / "image-jobs" / str(index)
    if saved["result"] is not None:
        shutil.rmtree(job, ignore_errors=True)
        return saved["result"]
    command = saved["command"]
    # Validation errors are tool results: the assistant can select another source/folder.
    try:
        folder, source = authorize_image(engine, request, command)
    except ValueError as exc:
        result = {"error": str(exc)}
        with state.db:
            state.db.execute("UPDATE ai_steps SET result=? WHERE note_id=? AND step=?",
                             (json.dumps(result, ensure_ascii=False), identity, index))
        return result
    path = await engine.provider.generate_image(command["content"], request["model"] or engine.model(owner),
                                                job, state.store.limits.max_file_bytes, source=source)
    # No awaits between rechecking access/ownership/quotas, file publication, and the receipt commit.
    folder, _ = authorize_image(engine, request, command)
    data = png_bytes(path, state.store.limits.max_file_bytes)
    title = clean_name(command["title"] or "Изображение ИИ")
    key = hashlib.sha256(f"image:{identity}:{index}".encode()).hexdigest()[:32]
    created_note = None
    try:
        with state.db:
            note, created = state.store.add_note(owner, folder, {"message_id": -(int(key[:15], 16) + 1)},
                                                "photo", extension="png", size=len(data), transcript="", commit=False)
            if not created:
                raise ValueError("Не удалось сохранить картинку: конфликт идентификатора")
            created_note = note
            state.db.execute("UPDATE notes SET state='ready',origin='web',original_name='image.png',"
                "mime_type='image/png',upload_key=?,audio_bytes=?,sha256=?,title=?,ai_generated_from=?,"
                "delivery='sent',transcript_delivery='sent',routing='manual',archive_dirty=1 WHERE id=? AND owner=?",
                (key, len(data), hashlib.sha256(data).hexdigest(), title, identity, note["id"], owner))
            note = state.store.note(owner, note["id"])
            target = engine.archive.audio(note)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            durable_bytes(target, data)
            result = {"ok": True, "action": command["action"], "id": note["id"], "title": title, "revision": 0}
            state.db.execute("UPDATE ai_steps SET result=? WHERE note_id=? AND step=?",
                             (json.dumps(result, ensure_ascii=False), identity, index))
    except BaseException:
        if created_note:
            engine.archive.delete(created_note)
        raise
    shutil.rmtree(job, ignore_errors=True)
    return result
