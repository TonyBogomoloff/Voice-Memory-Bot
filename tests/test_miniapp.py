import asyncio
import hashlib
import hmac
import io
import json
import logging
import time
import wave
import zipfile
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import urlencode

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_mvp import make_bot
from test_mvp import store as store

from voice_memory_bot.config import Limits
from voice_memory_bot.miniapp import MiniApp, validate_init_data
from voice_memory_bot.store import Store

TOKEN = "123:secret"


def init_data(owner=1, age=0, **extra):
    fields = {"auth_date": str(int(time.time()) - age), "query_id": "query",
              "user": json.dumps({"id": owner, "first_name": "Test"}), **extra}
    data = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    key = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
    fields["hash"] = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.mark.parametrize("raw", ["", "hash=bad", init_data(age=3605), init_data(age=-90),
    init_data(owner=2), init_data() + "&auth_date=1", init_data().replace("query", "tampered", 1)])
def test_rejects_invalid_expired_duplicate_or_unapproved_identity(raw):
    with pytest.raises(web.HTTPUnauthorized):
        validate_init_data(raw, TOKEN, {1})


def test_validates_current_telegram_signature_including_signature_field():
    assert validate_init_data(init_data(signature="telegram-ed25519-field"), TOKEN, {1}) == 1


@asynccontextmanager
async def client_for(tmp_path, store):
    bot = make_bot(tmp_path, store)
    bot.config = replace(bot.config, allowed_user_ids=frozenset({1, 2}),
                         web_url="https://audio-notes.example.test")
    mini = bot.web = MiniApp(bot)
    async with TestClient(TestServer(mini.application())) as client:
        result = await client.post("/api/session", json={"init_data": init_data()})
        assert result.status == 200
        client.session.headers["Authorization"] = "Bearer " + (await result.json())["token"]
        yield bot, mini, client


async def upload(client, folder, data=b"private attachment", name="notes.txt", mime="text/plain", key="a" * 32,
                 voice=False):
    return await client.put(f"/api/folders/{folder}/uploads/{key}", data=data,
        headers={"X-File-Name": name, "Content-Type": mime, **({"X-Media-Kind": "voice"} if voice else {})})


def test_archive_upload_move_media_permissions_idempotency_and_export(tmp_path, store):
    async def run():
        async with client_for(tmp_path, store) as (bot, mini, client):
            first = (await (await client.get("/api/folders")).json())["folders"][0]["id"]
            created = await client.post("/api/folders", json={"name": "Files"})
            assert created.status == 201
            second = (await created.json())["id"]
            assert store.folder(1, second)["thread_id"] is None
            response = await upload(client, first, name="../../private.html", mime="text/html")
            assert response.status == 201
            public = await response.json()
            assert public["original_name"] == "private.html"
            assert not {"owner", "file_id", "upload_key", "message_id", "sha256"} & public.keys()
            duplicate = await upload(client, first)
            assert (await duplicate.json())["id"] == public["id"]
            assert len(store.notes(1)) == 1
            await bot.process(store.claim_note())
            note = store.note(1, public["id"])
            assert note["state"] == "ready" and note["origin"] == "web"
            assert not bot.telegram.sent and bot.groq.calls == 0
            assert bot.archive.audio(note).read_bytes() == b"private attachment"
            moved = await client.post(f"/api/notes/{note['id']}/move", json={"folder_id": second})
            assert moved.status == 200 and store.note(1, note["id"])["folder_id"] == second
            assert store.note(1, note["id"])["revision"] == 1
            ticket = (await (await client.get(f"/api/notes/{note['id']}/media")).json())["url"]
            media = await client.get(ticket, headers={"Range": "bytes=0-6"})
            assert media.status == 206 and await media.read() == b"private"
            assert media.headers["Content-Type"] == "application/octet-stream"
            assert media.headers["Content-Disposition"].startswith("attachment")
            assert media.headers["Cache-Control"] == "no-store"
            native = await client.get(ticket + "&download=1", headers={"Origin": "https://web.telegram.org"})
            assert native.status == 200 and await native.read() == b"private attachment"
            assert native.headers["Access-Control-Allow-Origin"] == "https://web.telegram.org"
            assert 'filename="private.html"' in native.headers["Content-Disposition"]
            forged = await client.get(ticket + "x")
            assert forged.status == 401
            missing_auth = await client.get("/api/folders", headers={"Authorization": ""})
            assert missing_auth.status == 401
            cross_origin = await client.post("/api/folders", json={"name": "bad"},
                                              headers={"Origin": "https://evil.example"})
            assert cross_origin.status == 403
            assert (await client.get(ticket, headers={"Origin": "https://evil.example"})).status == 403
            assert (await client.get(ticket + "x", headers={"Origin": "https://web.telegram.org"})).status == 401
            assert (await client.post("/api/folders", json={"name": "bad"},
                                      headers={"Origin": "https://web.telegram.org"})).status == 403
            client.session.headers["Authorization"] = "Bearer " + mini.sign("session", 2)
            assert (await client.get(f"/api/folders/{second}/notes")).status == 400
            assert (await client.post(f"/api/notes/{note['id']}/move",
                                     json={"folder_id": store.ensure_user(2)})).status == 400
            assert (await client.get(f"/api/notes/{note['id']}/media")).status == 400
            owner2_ticket = mini.sign(f"media:{note['id']}", 2)
            assert (await client.get(f"/media/{note['id']}?ticket={owner2_ticket}")).status == 400
            note = store.note(1, note["id"])
            paths = bot.archive.export([note], store.folders(1), tmp_path / "zip", True, 100000,
                                       text_format="markdown")
            with zipfile.ZipFile(paths[0]) as archive:
                assert any("/files/" in n and n.endswith(".html") for n in archive.namelist())
                assert archive.read(next(n for n in archive.namelist() if "/files/" in n)) == b"private attachment"
            assert bot.archive.export([note], store.folders(1), tmp_path / "audio", True,
                                      100000, audio_only=True) == []
            store.delete(1, note["id"])
            assert (await client.get(ticket)).status == 400
    asyncio.run(run())


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 16000)
    return output.getvalue()


def test_voice_pipeline_recovers_after_restart_and_preserves_selected_folder(tmp_path, store):
    async def run():
        async with client_for(tmp_path, store) as (bot, mini, client):
            folder = store.create_folder(1, "Selected")
            store.create_folder(1, "Игра")
            response = await upload(client, folder["id"], wav_bytes(), "record.wav", "audio/wav", voice=True)
            assert response.status == 201
            public = await response.json()
            claimed = store.claim_note()
            assert claimed["state"] == "processing"
            # Reopen a copy as if the previous process stopped after claiming its durable job.
            recovery_path = tmp_path / "recovery.sqlite3"
            import sqlite3
            with sqlite3.connect(recovery_path) as destination:
                store.db.backup(destination)
            recovered = Store(recovery_path, Limits())
            recovered_bot = make_bot(tmp_path, recovered)
            try:
                assert recovered.note(1, public["id"])["state"] == "queued"
                await recovered_bot.process(recovered.claim_note())
                note = recovered.note(1, public["id"])
                assert note["state"] == "ready" and "Игра" in note["transcript"]
                assert note["folder_id"] == folder["id"] and note["duration"] > 0
                assert recovered_bot.groq.calls == 1 and not recovered_bot.telegram.sent
                assert not (recovered_bot.archive.directory(note) / "asr.mp3").exists()
                assert recovered_bot.archive.audio(note).read_bytes() == wav_bytes()
            finally:
                recovered.close()
    asyncio.run(run())


@pytest.mark.parametrize("mime,name,kind", [("image/png", "photo.png", "photo"),
    ("video/mp4", "video.mp4", "video"), ("application/pdf", "document.pdf", "document")])
def test_non_audio_uploads_never_use_asr(tmp_path, store, mime, name, kind):
    async def run():
        async with client_for(tmp_path, store) as (bot, mini, client):
            response = await upload(client, store.ensure_user(1), name=name, mime=mime)
            assert response.status == 201
            note = await response.json()
            await bot.process(store.claim_note())
            saved = store.note(1, note["id"])
            assert saved["kind"] == kind and saved["state"] == "ready"
            assert bot.groq.calls == 0 and not bot.telegram.sent
    asyncio.run(run())


def test_size_quota_and_cleanup_protect_uploads(tmp_path, store):
    async def run():
        async with client_for(tmp_path, store) as (bot, mini, client):
            folder = store.ensure_user(1)
            mini.config = replace(mini.config, limits=replace(mini.config.limits, max_file_bytes=5))
            assert (await upload(client, folder)).status == 413
            assert (await upload(client, folder, data=b"", key="b" * 32)).status == 400
            assert not store.notes(1)
            assert not list((tmp_path / "uploads").glob("*.part"))
            mini.uploading.add(1)
            assert bot.deletion_busy(1)
            assert (await upload(client, folder, data=b"123")).status == 409
            mini.uploading.clear()
            store.limits = replace(store.limits, max_user_storage_bytes=2)
            assert (await upload(client, folder, data=b"123")).status == 409
            assert not store.notes(1)
    asyncio.run(run())


def test_failed_asr_retains_original_and_retry_does_not_send_telegram(tmp_path, store):
    async def run():
        async with client_for(tmp_path, store) as (bot, mini, client):
            folder = store.ensure_user(1)
            bot.groq.error = "groq_unavailable"
            response = await upload(client, folder, wav_bytes(), "record.wav", "audio/wav", voice=True)
            identity = (await response.json())["id"]
            await bot.process(store.claim_note())
            note = store.note(1, identity)
            assert note["state"] == "asr_failed" and bot.archive.audio(note).exists()
            bot.groq.error = None
            assert (await client.post(f"/api/notes/{identity}/retry", json={})).status == 200
            await bot.process(store.claim_note())
            assert store.note(1, identity)["state"] == "ready" and not bot.telegram.sent
    asyncio.run(run())


def test_app_command_opens_registered_url_but_home_keeps_folder_keyboard(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        bot.config = replace(bot.config, web_url="https://audio-notes.example.test")
        await bot.command(1, None, "/app", "")
        assert bot.telegram.sent[-1][1]["keyboard"]["keyboard"][0][0]["web_app"]["url"] == bot.config.web_url
        await bot.command(1, None, "/start", "")
        menu = next(p for m, p in reversed(bot.telegram.sent) if m == "sendMessage")["keyboard"]
        assert menu["is_persistent"] and menu["keyboard"][0][0]["text"] == "✓ 📌 Входящие"
        assert "web_app" not in str(menu)
        await bot.configure_menu()
        assert all(p["menu_button"] == {"type": "commands"} for m, p in bot.telegram.sent if m == "setChatMenuButton")
    asyncio.run(run())


def test_real_server_startup_never_logs_media_tickets(tmp_path, store, caplog):
    async def run():
        bot = make_bot(tmp_path, store)
        bot.config = replace(bot.config, web_port=0)
        mini = bot.web = MiniApp(bot)
        caplog.set_level(logging.INFO, logger="aiohttp.access")
        await mini.start()
        try:
            host, port = mini.runner.addresses[0]
            async with httpx.AsyncClient(base_url=f"http://{host}:{port}", trust_env=False) as client:
                assert (await client.get("/healthz")).status_code == 200
                assert (await client.get("/media/test?ticket=never-log-this-ticket")).status_code == 401
                assert (await client.post("/api/session", json={"init_data": init_data()})).status_code == 200
            assert not [r for r in caplog.records if r.name == "aiohttp.access"]
            assert not [r for r in caplog.records if "falling back to filtered kwargs" in r.message]
        finally:
            await mini.close()
    asyncio.run(run())


def test_text_execution_keeps_source_and_commits_one_reply(tmp_path, store):
    from test_ai import Assistant, step

    async def run():
        async with client_for(tmp_path, store) as (bot, mini, client):
            provider = Assistant()
            bot.ai_engine.provider = provider
            folder = store.ensure_user(1)
            source = await client.put(f"/api/folders/{folder}/text/{'b' * 32}", json={"text": "Итоги: встреча в среду."})
            original = await source.json()
            assert source.status == 201 and original["can_execute"]
            # Include a long Cyrillic body: the limit is characters, not JSON bytes.
            text = "Сделай summary выбранной заметки. " + "Дополнение. " * 3000
            response = await client.put(f"/api/folders/{folder}/text/{'c' * 32}",
                                        json={"text": text, "context_note_id": original["id"]})
            assert response.status == 201
            command = await response.json()
            duplicate = await client.put(f"/api/folders/{folder}/text/{'c' * 32}", json={"text": text})
            assert (await duplicate.json())["id"] == command["id"]
            provider.responses = [step("read_note", note_id=original["id"]), step(message="Встреча — в среду.")]
            for _ in range(2):
                response = await client.post(f"/api/notes/{command['id']}/execute")
                assert response.status == 202 and (await response.json())["ai"]["state"] == "queued"
            assert store.one("SELECT count(*) AS n FROM ai_requests")["n"] == 1
            await bot.process_ai(bot.ai_state.claim())
            request = bot.ai_state.request(1, command["id"])
            assert request["state"] == "ready" and request["delivery"] == "sent"
            assert provider.calls[0][0]["context_note_id"] == original["id"]
            assert provider.calls[1][0]["steps"][0]["result"]["content"] == original["text"]
            assert store.note(1, command["id"])["entry_type"] == "note"
            assert store.note(1, command["id"])["transcript"] == text
            reply = store.note(1, request["result_note_id"])
            assert reply["folder_id"] == folder and reply["transcript"] == "Встреча — в среду."
            assert reply["origin"] == "web" and mini.public_note(reply)["ai_generated"]
            await client.post(f"/api/notes/{command['id']}/execute")
            await bot.deliver_ai(request)
            assert len(store.notes(1)) == 3 and not bot.telegram.sent and bot.groq.calls == 0
            client.session.headers["Authorization"] = "Bearer " + mini.sign("session", 2)
            assert (await client.post(f"/api/notes/{command['id']}/execute")).status == 400
            second_folder = store.ensure_user(2)
            assert (await client.put(f"/api/folders/{second_folder}/text/{'d' * 32}",
                                    json={"text": "Команда", "context_note_id": original["id"]})).status == 400
            store.delete(1, command["id"])
            assert store.one("SELECT input_text FROM ai_requests WHERE note_id=?", (command["id"],))["input_text"] is None
    asyncio.run(run())


def test_failed_execution_retries_without_duplicate_created_note(tmp_path, store):
    from test_ai import Assistant, step

    from voice_memory_bot.providers import ProviderError

    async def run():
        async with client_for(tmp_path, store) as (bot, mini, client):
            provider = Assistant()
            bot.ai_engine.provider = provider
            folder = store.ensure_user(1)
            response = await client.put(f"/api/folders/{folder}/text/{'e' * 32}", json={"text": "Исходный текст"})
            identity = (await response.json())["id"]
            with store.db:
                store.db.execute("UPDATE notes SET content=? WHERE id=?", ("Создай заметку о встрече", identity))
            provider.responses = [step("create_note", folder_id=folder, title="Встреча", content="В среду"),
                                  ProviderError("temporary"), step(message="Создана заметка о встрече.")]
            await client.post(f"/api/notes/{identity}/execute")
            await bot.process_ai(bot.ai_state.claim())
            assert bot.ai_state.request(1, identity)["state"] == "failed"
            assert provider.calls[0][0]["request"] == "Создай заметку о встрече"
            assert len(store.notes(1)) == 2
            assert (await client.post(f"/api/notes/{identity}/execute")).status == 202
            await bot.process_ai(bot.ai_state.claim())
            assert bot.ai_state.request(1, identity)["state"] == "ready"
            assert len(store.notes(1)) == 3
            assert sum(n["title"] == "Встреча" for n in store.notes(1)) == 1
            assert not bot.telegram.sent
    asyncio.run(run())


def test_image_is_attached_only_after_owner_scoped_read(tmp_path, store):
    from test_ai import Assistant, step

    class Vision(Assistant):
        async def respond(self, payload, model, images=()):
            if images:
                self.seen = [path.read_bytes() for path in images]
            return await super().respond(payload, model)

    async def run():
        async with client_for(tmp_path, store) as (bot, mini, client):
            provider = Vision()
            bot.ai_engine.provider = provider
            folder = store.ensure_user(1)
            response = await upload(client, folder, b"synthetic image", "photo.png", "image/png")
            photo = await response.json()
            await bot.process(store.claim_note())
            response = await client.put(f"/api/folders/{folder}/text/{'f' * 32}",
                                        json={"text": "Что на фото?", "context_note_id": photo["id"]})
            identity = (await response.json())["id"]
            provider.responses = [step("read_note", note_id=photo["id"]), step(message="Описание картинки")]
            await client.post(f"/api/notes/{identity}/execute")
            await bot.process_ai(bot.ai_state.claim())
            assert provider.seen == [b"synthetic image"]
            assert bot.ai_state.request(1, identity)["state"] == "ready"
            bot.ai_state.set_settings(1, notes_access="none")
            assert bot.ai_engine.images(bot.ai_state.request(1, identity), bot.ai_state.steps(1, identity)) == []
    asyncio.run(run())
