from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote

from aiohttp import web

from .providers import ProviderError
from .web_store import create_text

log = logging.getLogger(__name__)
ASSETS = Path(__file__).with_name("web")
AUDIO = {"ogg", "opus", "mp3", "mp4", "mpeg", "mpga", "m4a", "aac", "flac", "wav", "webm"}
PHOTOS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
VIDEOS = {"video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov"}
OWNER = web.RequestKey("owner", int)


def validate_init_data(raw, token, allowed, now=None):
    """Telegram HMAC validation, never trust initDataUnsafe or a caller's user ID."""
    try:
        if not isinstance(raw, str) or not 1 <= len(raw) <= 16384:
            raise ValueError
        pairs = parse_qsl(raw, strict_parsing=True, max_num_fields=30)
        fields = dict(pairs)
        if len(pairs) != len(fields):
            raise ValueError
        supplied = fields.pop("hash")
        key = hmac.digest(b"WebAppData", token.encode(), "sha256")
        data = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        expected = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError
        age = (time.time() if now is None else now) - int(fields["auth_date"])
        if not -30 <= age <= 3600:
            raise ValueError
        user = json.loads(fields["user"])
        owner = user["id"]
        if type(owner) is not int or owner not in allowed or user.get("is_bot"):
            raise ValueError
        return owner
    except (ValueError, KeyError, TypeError, OverflowError):
        raise web.HTTPUnauthorized(text="Открой мини-эп заново из меню бота.") from None


class MiniApp:
    def __init__(self, bot):
        self.bot, self.store, self.config = bot, bot.store, bot.config
        self.key = hmac.digest(self.config.bot_token.encode(), b"voice-memory-miniapp-v1", "sha256")
        self.uploading = set()
        self.recent = defaultdict(deque)
        self.runner = None

    def sign(self, scope, owner, lifetime=28800):
        data = f"{owner}.{int(time.time()) + lifetime}.{secrets.token_hex(8)}"
        signature = hmac.new(self.key, f"{scope}:{data}".encode(), hashlib.sha256).hexdigest()
        return f"{data}.{signature}"

    def verify(self, scope, token):
        try:
            owner, expires, nonce, signature = token.split(".")
            data = f"{owner}.{expires}.{nonce}"
            expected = hmac.new(self.key, f"{scope}:{data}".encode(), hashlib.sha256).hexdigest()
            if (not hmac.compare_digest(signature, expected) or int(expires) < time.time()
                    or int(owner) not in self.config.allowed_user_ids):
                raise ValueError
            return int(owner)
        except (ValueError, AttributeError):
            raise web.HTTPUnauthorized(text="Открой мини-эп заново из меню бота.") from None

    def owner(self, request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise web.HTTPUnauthorized(text="Открой мини-эп из меню бота.")
        return self.verify("session", header[7:])

    def available(self, owner):
        job = self.bot.cleanup.job(owner)
        if owner in self.bot.deleting_owners or job and job["files_pending"]:
            raise web.HTTPConflict(text="Идёт очистка архива. Подожди её завершения.")

    def rate(self, owner, limit=180):
        now, recent = time.monotonic(), self.recent[owner]
        while recent and recent[0] < now - 60:
            recent.popleft()
        if len(recent) >= limit:
            raise web.HTTPTooManyRequests(text="Слишком много запросов. Подожди минуту.")
        recent.append(now)

    @web.middleware
    async def guard(self, request, handler):
        media_download = request.method in {"GET", "HEAD"} and request.path.startswith("/media/")
        try:
            origin = request.headers.get("Origin")
            if origin not in (None, self.config.web_url) and not (
                media_download and origin == "https://web.telegram.org"
            ):
                raise web.HTTPForbidden(text="Недопустимый источник запроса.")
            if request.path.startswith("/api/") and request.path != "/api/session":
                owner = self.owner(request)
                self.available(owner)
                self.rate(owner)
                request[OWNER] = owner
            async with asyncio.timeout(125):
                response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({"error": exc.text}, status=exc.status)
        except (ValueError, KeyError, TypeError):
            response = web.json_response({"error": "Запись или папка недоступна. Обнови список и попробуй снова."},
                                         status=400)
        except TimeoutError:
            response = web.json_response({"error": "Время ожидания истекло. Попробуй ещё раз."}, status=408)
        except Exception as exc:
            log.error("miniapp request failed type=%s", type(exc).__name__)
            response = web.json_response({"error": "Не удалось выполнить действие. Попробуй ещё раз."}, status=500)
        self.headers(response)
        if media_download:
            response.headers["Access-Control-Allow-Origin"] = "https://web.telegram.org"
        return response

    @staticmethod
    def headers(response):
        response.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Permissions-Policy": "microphone=(self), camera=()",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' https://telegram.org; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' blob:; media-src 'self' blob:; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org",
        })

    def application(self):
        app = web.Application(middlewares=[self.guard], client_max_size=512000)
        app.add_routes([
            web.get("/", self.index), web.get("/app.js", self.asset), web.get("/app.css", self.asset),
            web.get("/healthz", self.health), web.post("/api/session", self.session),
            web.get("/api/folders", self.folders), web.post("/api/folders", self.create_folder),
            web.get("/api/folders/{folder}/notes", self.notes),
            web.put("/api/folders/{folder}/uploads/{key}", self.upload),
            web.put("/api/folders/{folder}/text/{key}", self.text_note),
            web.post("/api/notes/{note}/execute", self.execute),
            web.post("/api/notes/{note}/move", self.move), web.post("/api/notes/{note}/retry", self.retry),
            web.get("/api/notes/{note}/media", self.media_url), web.get("/media/{note}", self.media),
        ])
        return app

    async def start(self):
        # Staging files have never been acknowledged as accepted notes.
        staging = self.config.data_dir / "uploads"
        staging.mkdir(exist_ok=True, mode=0o700)
        for path in staging.glob("*.part"):
            path.unlink()
        self.runner = web.AppRunner(self.application(), access_log=None, shutdown_timeout=10,
                                    keepalive_timeout=30, auto_decompress=False)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.config.web_port).start()

    async def close(self):
        if self.runner:
            await self.runner.cleanup()

    async def index(self, request):
        return web.Response(body=(ASSETS / "index.html").read_bytes(), content_type="text/html")

    async def asset(self, request):
        name = request.path.lstrip("/")
        return web.Response(body=(ASSETS / name).read_bytes(),
                            content_type="text/css" if name.endswith(".css") else "text/javascript")

    async def health(self, request):
        self.store.one("SELECT 1")
        return web.json_response({"status": "ok"})

    async def session(self, request):
        body = await request.json()
        owner = validate_init_data(body.get("init_data"), self.config.bot_token, self.config.allowed_user_ids)
        self.available(owner)
        self.rate(owner)
        self.store.ensure_user(owner)
        return web.json_response({"token": self.sign("session", owner),
            "max_file_bytes": self.config.limits.max_file_bytes,
            "max_audio_seconds": self.config.limits.max_audio_seconds})

    async def folders(self, request):
        owner = request[OWNER]
        self.store.ensure_user(owner)
        return web.json_response({"folders": [
            {k: f[k] for k in ("id", "name", "count", "is_inbox")} for f in self.store.folders(owner)]})

    async def create_folder(self, request):
        body = await request.json()
        try:
            folder = self.store.create_folder(request[OWNER], body["name"])
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        # Virtual folders need no Telegram topic and are immediately usable in both UIs.
        return web.json_response({"id": folder["id"], "name": folder["name"]}, status=201)

    def public_note(self, note):
        result = {k: note[k] for k in ("id", "folder_id", "title", "kind", "state", "created_at",
                                      "duration", "audio_bytes", "original_name", "mime_type")}
        result["text"] = note["content"] if note.get("content") is not None else note["transcript"]
        result["has_file"] = bool(note["audio_bytes"])
        result["can_move"] = note["state"] not in {"queued", "processing"}
        result["can_retry"] = note["state"] == "asr_failed" and note.get("origin") == "web" and note["attempts"] < 5
        result["ai_generated"] = bool(note.get("ai_generated_from"))
        job = self.bot.ai_state.request(note["owner"], note["id"])
        result["ai"] = ({k: job[k] for k in ("state", "result_note_id")} if job else None)
        if job and job["state"] == "running":
            steps = self.bot.ai_state.steps(note["owner"], note["id"])
            if steps and steps[-1]["command"]["action"] in {"create_image", "edit_image"} and not steps[-1]["result"]:
                result["ai"]["phase"] = "image"
        if job and job["state"] == "failed":
            result["ai"]["error"] = job["response"]
        result["can_execute"] = (note["kind"] in {"voice", "audio", "text"} and note["state"] == "ready"
            and bool((result["text"] or "").strip()) and (not job or job["state"] == "failed"))
        return result

    async def text_note(self, request):
        owner, folder, key = request[OWNER], request.match_info["folder"], request.match_info["key"]
        if not re.fullmatch(r"[0-9a-f]{32}", key):
            raise web.HTTPBadRequest(text="Неверный идентификатор записи.")
        body = await request.json()
        try:
            with self.store.db:
                note, created = create_text(self.store, owner, folder, body.get("text"), key,
                                            context=body.get("context_note_id"))
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        return web.json_response(self.public_note(note), status=201 if created else 200)

    async def execute(self, request):
        owner, identity = request[OWNER], request.match_info["note"]
        note = self.store.note(owner, identity)
        text = note["content"] if note["content"] is not None else note["transcript"]
        if (note["kind"] not in {"voice", "audio", "text"} or note["state"] != "ready"
                or not text or not text.strip() or len(text) > 40000):
            raise web.HTTPConflict(text="Выполнить можно готовый текст или расшифрованное голосовое до 40000 символов.")
        try:
            model = self.bot.ai_engine.model(owner)
            job = self.bot.ai_state.enqueue(owner, identity, "miniapp")
            if job["state"] == "failed":
                self.bot.ai_state.update_request(owner, identity, state="queued", response=None, error=None,
                                                 delivery="pending", model=model)
            elif not job["model"]:
                self.bot.ai_state.update_request(owner, identity, model=model)
        except ValueError as exc:
            raise web.HTTPConflict(text=str(exc)) from None
        return web.json_response(self.public_note(self.store.note(owner, identity)), status=202)

    async def notes(self, request):
        offset = max(0, min(int(request.query.get("offset", 0)), self.config.limits.max_notes_per_user))
        notes = self.store.notes(request[OWNER], request.match_info["folder"], offset=offset, limit=41)
        return web.json_response({"notes": [self.public_note(n) for n in notes[:40]], "more": len(notes) > 40})

    def upload_metadata(self, request):
        filename = unquote(request.headers.get("X-File-Name", "Файл"))
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        filename = "".join(c for c in filename if c.isprintable())[:180].strip() or "Файл"
        mime = request.content_type.lower()
        extension = Path(filename).suffix.lower().lstrip(".")
        if not re.fullmatch(r"[a-z0-9]{1,12}", extension):
            extension = "bin"
        if request.headers.get("X-Media-Kind") == "voice":
            if extension not in AUDIO:
                raise web.HTTPBadRequest(text="Не удалось определить формат голосовой записи.")
            kind = "voice"
        elif mime in PHOTOS:
            kind, extension = "photo", PHOTOS[mime]
        elif mime in VIDEOS:
            kind, extension = "video", VIDEOS[mime]
        elif mime.startswith("audio/") and extension in AUDIO:
            kind = "audio"
        else:
            kind, mime = "document", "application/octet-stream"
        return filename, mime, extension, kind

    async def upload(self, request):
        owner, folder, key = request[OWNER], request.match_info["folder"], request.match_info["key"]
        if not re.fullmatch(r"[0-9a-f]{32}", key):
            raise web.HTTPBadRequest(text="Неверный идентификатор загрузки.")
        self.store.folder(owner, folder)
        old = self.store.one("SELECT * FROM notes WHERE owner=? AND upload_key=?", (owner, key))
        if old:
            if old["deleted"]:
                raise web.HTTPConflict(text="Эта загрузка уже была удалена.")
            return web.json_response(self.public_note(old))
        if owner in self.uploading:
            raise web.HTTPConflict(text="Предыдущий файл ещё загружается.")
        maximum = self.config.limits.max_file_bytes
        if request.content_length and request.content_length > maximum:
            raise web.HTTPRequestEntityTooLarge(max_size=maximum, actual_size=request.content_length,
                                                text=f"Файл должен быть не больше {maximum // 1000000} МБ.")
        filename, mime, extension, kind = self.upload_metadata(request)
        context = request.headers.get("X-Context-Note") or None
        if context:
            self.store.note(owner, context)
        staging = self.config.data_dir / "uploads"
        staging.mkdir(exist_ok=True, mode=0o700)
        self.uploading.add(owner)
        path, note = None, None
        try:
            with tempfile.NamedTemporaryFile(dir=staging, suffix=".part", delete=False) as handle:
                path = Path(handle.name)
                size, digest = 0, hashlib.sha256()
                async with asyncio.timeout(120):
                    async for chunk in request.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > maximum:
                            raise web.HTTPRequestEntityTooLarge(max_size=maximum, actual_size=size,
                                                                text="Файл слишком большой.")
                        digest.update(chunk)
                        handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size == 0:
                raise web.HTTPBadRequest(text="Пустой файл не загружен.")
            # Recheck after streaming: deletion and quota usage may have changed meanwhile.
            self.available(owner)
            self.store.folder(owner, folder)
            if context:
                self.store.note(owner, context)
            message_id = -(int(key[:15], 16) + 1)
            try:
                with self.store.db:
                    note, created = self.store.add_note(owner, folder, {"message_id": message_id}, kind,
                        extension=extension, size=size, transcript=None if kind in {"voice", "audio"} else "",
                        commit=False)
                    if not created:
                        note = None
                        raise web.HTTPConflict(text="Повтори загрузку с новым идентификатором.")
                    self.store.db.execute("UPDATE notes SET origin='web',original_name=?,mime_type=?,upload_key=?,ai_context=?,"
                        "audio_bytes=?,sha256=?,title=?,delivery='sent',transcript_delivery='sent',routing='manual' "
                        "WHERE id=? AND owner=?", (filename, mime, key, context, size, digest.hexdigest(), filename,
                                                  note["id"], owner))
                    note = self.store.note(owner, note["id"])
                    target = self.bot.archive.audio(note)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    path.replace(target)
                    directory_fd = os.open(target.parent, os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            except ValueError as exc:
                raise web.HTTPConflict(text=str(exc)) from None
            # Commit only after the file is durable; the worker never sees a partial upload.
            return web.json_response(self.public_note(note), status=201)
        except BaseException:
            if note:
                with self.store.db:
                    self.store.db.execute("DELETE FROM notes WHERE id=? AND owner=?", (note["id"], owner))
                self.bot.archive.delete(note)
            raise
        finally:
            if path:
                path.unlink(missing_ok=True)
            self.uploading.discard(owner)

    async def move(self, request):
        owner, identity = request[OWNER], request.match_info["note"]
        body = await request.json()
        note = self.store.note(owner, identity)
        folder = self.store.folder(owner, body["folder_id"])
        if self.bot.note_locks[identity].locked() or note["state"] in {"queued", "processing"}:
            raise web.HTTPConflict(text="Дождись завершения обработки и попробуй снова.")
        previous = self.bot.messages.get(owner, identity)
        if previous and previous["state"] not in {"done", "blocked"}:
            raise web.HTTPConflict(text="Сначала заверши перенос этой записи в боте.")
        async with self.bot.note_locks[identity]:
            self.store.update_note(owner, identity, folder_id=folder["id"], routing="manual", archive_dirty=1)
        return web.json_response(self.public_note(self.store.note(owner, identity)))

    async def retry(self, request):
        note = self.store.note(request[OWNER], request.match_info["note"])
        if note["origin"] != "web":
            raise web.HTTPConflict(text="Повтори обработку этой записи через бота.")
        self.store.retry(note["owner"], note["id"])
        self.store.update_note(note["owner"], note["id"], delivery="sent", transcript_delivery="sent")
        return web.json_response({"ok": True})

    async def media_url(self, request):
        note = self.store.note(request[OWNER], request.match_info["note"])
        if not note["audio_bytes"]:
            raise web.HTTPConflict(text="Файл ещё загружается.")
        token = self.sign(f"media:{note['id']}", note["owner"], lifetime=7200)
        return web.json_response({"url": f"/media/{note['id']}?ticket={token}"})

    async def media(self, request):
        identity = request.match_info["note"]
        owner = self.verify(f"media:{identity}", request.query.get("ticket", ""))
        self.available(owner)
        note = self.store.note(owner, identity)
        path = self.bot.archive.audio(note)
        if not note["audio_bytes"] or not path.is_file():
            raise web.HTTPNotFound(text="Файл пока недоступен.")
        mime = note["mime_type"] or ({"photo": "image/jpeg", "voice": "audio/ogg"}.get(note["kind"]))
        if not mime:
            mime = {"mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4",
                    "webm": "audio/webm", "flac": "audio/flac", "ogg": "audio/ogg"}.get(note["extension"])
        safe = mime in {*PHOTOS, *VIDEOS, "audio/ogg", "audio/mpeg", "audio/mp4", "audio/webm",
                        "audio/flac", "audio/wav", "audio/x-wav", "audio/aac"}
        inline = safe and request.query.get("download") != "1"
        filename = note["original_name"] or f"{note['id']}.{note['extension']}"
        fallback = re.sub(r"[^a-zA-Z0-9._-]", "_", filename) or "attachment"
        response = web.FileResponse(path, headers={"Content-Type": mime if safe else "application/octet-stream",
            "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename=\"{fallback}\"; "
                f"filename*=UTF-8''{quote(filename, safe='')}"})
        return response


async def prepare_audio(bot, note):
    """MediaRecorder WebM may have no duration. Decode a bounded, ASR-sized copy."""
    target = bot.archive.directory(note) / "asr.mp3"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe",
        "-format_whitelist", "ogg,mp3,wav,flac,mov,matroska,webm,mpeg,aac", "-max_alloc", "67108864",
        "-i", str(bot.archive.audio(note)), "-map", "0:a:0", "-t", str(bot.config.limits.max_audio_seconds + 1),
        "-vn", "-ac", "1", "-ar", "16000", "-codec:a", "libmp3lame", "-b:a", "48k", str(target),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(process.wait(), 120)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ProviderError("audio_format_invalid")
    duration = await bot.probe_duration(target)
    return target, duration


async def process_web_note(bot, note):
    owner, identity = note["owner"], note["id"]
    temporary = bot.archive.directory(note) / "asr.mp3"
    try:
        path = bot.archive.audio(note)
        if not path.is_file() or path.stat().st_size != note["audio_bytes"]:
            raise ProviderError("upload_missing")
        if note["kind"] in {"voice", "audio"} and note["transcript"] is None:
            audio, duration = await prepare_audio(bot, note)
            bot.store.reserve_asr(owner, duration)
            transcript = await bot.groq.transcribe(audio)
            if not transcript:
                raise ProviderError("no_speech")
            bot.store.update_note(owner, identity, transcript=transcript, duration=duration,
                                  title="Голосовое сообщение" if note["kind"] == "voice" else note["original_name"])
        bot.store.update_note(owner, identity, state="ready", error=None, archive_dirty=1,
                              delivery="sent", transcript_delivery="sent")
    except (ProviderError, ValueError, OSError, TimeoutError) as exc:
        code = str(exc) if isinstance(exc, ProviderError) or str(exc) == "daily_asr_limit" else type(exc).__name__
        bot.store.update_note(owner, identity, state="asr_failed", error=code,
                              delivery="sent", transcript_delivery="sent")
        log.warning("miniapp processing failed note_id=%s error=%s", identity, code)
    finally:
        temporary.unlink(missing_ok=True)
