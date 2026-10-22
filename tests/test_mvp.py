import asyncio
import hashlib
import json
import wave
import zipfile
from dataclasses import replace

import httpx
import pytest

from voice_memory_bot.archive import Archive
from voice_memory_bot.bot import Bot
from voice_memory_bot.config import Config, Limits, LLMConfig
from voice_memory_bot.providers import CodexRouter, Groq, ProviderError, Telegram
from voice_memory_bot.routing import decide
from voice_memory_bot.store import Store


@pytest.fixture
def store(tmp_path):
    database = Store(tmp_path / "archive.sqlite3", Limits())
    database.ensure_user(1)
    database.ensure_user(2)
    yield database
    database.close()


def add(store, owner=1, message=10, **kwargs):
    folder = store.ensure_user(owner)
    return store.add_note(owner, folder, {"message_id": message}, **kwargs)[0]


def route_result(folder, **overrides):
    return {"title": "Идея интерфейса", "explicit": True, "ambiguous": False,
            "folder_id": folder["id"], "requested_folder": folder["name"],
            "evidence": f"Сохрани в проект {folder['name']}", **overrides}


def test_explicit_start_and_end_aliases_and_plain_mention(store):
    inbox = store.ensure_user(1)
    game = store.create_folder(1, "Игра")
    work = store.create_folder(1, "Работа")
    store.aliases(1, game["id"], ["мой игровой проект"])
    folders = store.folders(1)
    for transcript in ["Сохрани в проект Игра. Новые кнопки.", "Новые кнопки. Сохрани в проект Игра"]:
        result = decide(transcript, folders, work["id"], inbox, route_result(game))
        assert result.folder_id == game["id"] and result.status == "explicit"
    phrase = "Запиши в мой игровой проект"
    result = decide(phrase, folders, work["id"], inbox,
                    route_result(game, requested_folder="мой игровой проект", evidence=phrase))
    assert result.folder_id == game["id"]
    result = decide("На работе мы обсуждали проект Игра", folders, work["id"], inbox,
                    route_result(game, explicit=False, folder_id=None, requested_folder=None, evidence=None))
    assert result.folder_id == work["id"] and result.status == "manual"


@pytest.mark.parametrize("change", [
    {"folder_id": "other-user-id"}, {"evidence": "выдуманная команда"},
    {"requested_folder": "чужая папка"}, {"explicit": "true"}, {"ambiguous": True},
])
def test_invalid_llm_routing_never_overrides_archive_unsafely(store, change):
    inbox = store.ensure_user(1)
    game = store.create_folder(1, "Игра")
    result = decide("Сохрани в проект Игра", store.folders(1), game["id"], inbox,
                    route_result(game, **change))
    assert result.folder_id == inbox and result.status == "review"


def test_user_isolation_alias_collision_and_cyrillic_search(store):
    note = add(store, kind="text", transcript="ВСТРЕЧА с Ёжиком")
    with pytest.raises(ValueError):
        store.note(2, note["id"])
    with pytest.raises(ValueError):
        store.update_note(2, note["id"], title="stolen")
    with pytest.raises(ValueError):
        store.update_note(1, note["id"], folder_id=store.ensure_user(2))
    assert store.notes(2, query="встреча") == []
    assert store.notes(1, query="встреча ежик")[0]["id"] == note["id"]
    work = store.create_folder(1, "Работа")
    store.aliases(1, work["id"], ["Бизнес"])
    with pytest.raises(ValueError):
        store.create_folder(1, "бизнес")


def test_restart_recovers_queue_but_does_not_repeat_unknown_delivery(tmp_path):
    path = tmp_path / "db"
    db = Store(path, Limits())
    db.ensure_user(1)
    event = {"update_id": 10, "message": {"message_id": 1, "from": {"id": 1},
             "chat": {"id": 1, "type": "private"}, "text": "идея"}}
    db.ingest([event], {1})
    assert db.offset() == 11
    assert db.pending_update() == event
    first = add(db, kind="text", transcript="идея")
    same, created = db.add_note(1, db.ensure_user(1), {"message_id": 10}, "text", transcript="идея")
    assert not created and same["id"] == first["id"]
    db.claim_note()
    db.update_note(1, first["id"], delivery="sending")
    db.close()
    db = Store(path, Limits())
    assert db.pending_update() == event
    assert db.note(1, first["id"])["state"] == "queued"
    assert db.note(1, first["id"])["delivery"] == "unknown"
    db.close()


def test_import_of_bot_created_topic_reuses_pending_folder(store):
    folder = store.create_folder(1, "Игра")
    assert store.import_topic(1, "Игра", 123)["id"] == folder["id"]
    assert len(store.folders(1)) == 2


def test_implicit_telegram_threads_are_not_new_projects(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        base = {"from": {"id": 1}, "chat": {"id": 1, "type": "private"}, "message_thread_id": 99}
        await bot.handle({"message": {**base, "message_id": 1, "forum_topic_created": {
            "name": "Новая заметка", "is_name_implicit": True}}})
        await bot.handle({"message": {**base, "message_id": 2, "text": "Идея для будущего"}})
        assert len(store.folders(1)) == 1
        assert store.notes(1)[0]["source_folder"] == store.ensure_user(1)
    asyncio.run(run())


def test_storage_and_asr_limits_are_enforced(store):
    store.limits = replace(store.limits, max_pending_per_user=1, daily_asr_seconds=10)
    add(store, kind="voice", file_id="test", size=100)
    with pytest.raises(ValueError, match="Очередь"):
        add(store, message=11, kind="voice", file_id="test", size=100)
    store.reserve_asr(1, 3)
    with pytest.raises(ValueError, match="daily_asr_limit"):
        store.reserve_asr(1, 1)


def test_export_preserves_original_and_raw_text_and_checks_corruption(tmp_path, store):
    note = add(store, kind="voice", file_id="not-for-export", extension="ogg", transcript="Сохрани в Игра. Мысль.")
    folder = store.create_folder(1, "../../Проект")
    archive = Archive(tmp_path / "files")
    audio = archive.audio(note)
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"unchanged audio")
    store.update_note(1, note["id"], folder_id=folder["id"], audio_bytes=15,
                      sha256=hashlib.sha256(audio.read_bytes()).hexdigest(), state="ready")
    note = store.note(1, note["id"])
    paths = archive.export([note], store.folders(1), tmp_path / "export", True, 45000000)
    with zipfile.ZipFile(paths[0]) as zipped:
        assert all(not name.startswith("/") and ".." not in name.split("/") for name in zipped.namelist())
        names = zipped.namelist()
        assert zipped.read(next(n for n in names if n.endswith("voice.ogg"))) == b"unchanged audio"
        assert zipped.read(next(n for n in names if n.endswith("transcript.txt"))).decode() == note["transcript"]
        assert b"not-for-export" not in zipped.read("manifest.json")
    audio.write_bytes(b"modified  audio")
    with pytest.raises(ValueError, match="checksum"):
        archive.export([note], store.folders(1), tmp_path / "bad-export", True, 45000000)


def test_export_split_text_only_and_delete(tmp_path, store):
    notes = [add(store, message=i, kind="text", transcript="x" * 1000) for i in range(3)]
    archive = Archive(tmp_path / "files")
    paths = archive.export(notes, store.folders(1), tmp_path / "export", False, 15000)
    assert len(paths) == 3
    assert all(p.stat().st_size < 15000 for p in paths)
    for note in notes:
        archive.persist_text(note, store.folder(1, note["folder_id"]))
    store.delete(1, notes[0]["id"])
    archive.delete(notes[0])
    assert not archive.directory(notes[0]).exists()
    with pytest.raises(ValueError):
        store.note(1, notes[0]["id"])


def test_provider_errors_do_not_expose_credentials_or_transcripts(tmp_path):
    async def run():
        def handler(request):
            if request.url.path.endswith("getMe"):
                return httpx.Response(400, json={"ok": False, "error_code": 400,
                                                "description": "SENSITIVE PROVIDER RESPONSE SECRET"})
            return httpx.Response(429, text="SENSITIVE PROVIDER RESPONSE")
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        groq = Groq("SECRET", client=client)
        path = tmp_path / "voice.ogg"
        path.write_bytes(b"audio")
        with pytest.raises(ProviderError) as error:
            await groq.transcribe(path)
        assert str(error.value) == "groq_429"
        telegram = Telegram("123:SECRET", client=client)
        with pytest.raises(ProviderError) as error:
            await telegram.call("getMe")
        assert "SECRET" not in str(error.value) and "SENSITIVE" not in str(error.value)
        await client.aclose()
    asyncio.run(run())


def test_download_checks_actual_size_and_keeps_no_partial_file(tmp_path):
    async def run():
        def handler(request):
            if request.url.path.endswith("getFile"):
                return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/a.ogg"}})
            return httpx.Response(200, content=b"way too long")
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        telegram = Telegram("123:SECRET", client=client)
        target = tmp_path / "voice.ogg"
        with pytest.raises(ProviderError, match="audio_too_large"):
            await telegram.download("id", target, 3)
        assert not target.exists() and not target.with_suffix(".part").exists()
        await client.aclose()
    asyncio.run(run())


def test_codex_uses_subscription_and_has_no_inherited_api_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("CODEX_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://untrusted.invalid")
    monkeypatch.setenv("CODEX_THREAD_ID", "parent")
    env = CodexRouter.environment()
    assert not {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_THREAD_ID"} & env.keys()
    command = CodexRouter(LLMConfig()).command(tmp_path)
    assert "--ignore-user-config" in command and "--ephemeral" in command
    assert "forced_login_method=\"chatgpt\"" in command
    assert "features.shell_tool=false" in command and "features.apps=false" in command


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.fail_delivery = False

    async def call(self, method, **kwargs):
        self.sent.append((method, kwargs))
        if method == "createForumTopic":
            return {"message_thread_id": 123}
        if self.fail_delivery and method.startswith("send"):
            raise ProviderError("telegram_transport_unknown")
        return {"message_id": len(self.sent)}

    async def text(self, owner, text, thread=None, keyboard=None):
        return await self.call("sendMessage", chat_id=owner, text=text, thread=thread, keyboard=keyboard)

    async def download(self, file_id, destination, max_bytes):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 16000)
        data = destination.read_bytes()
        return len(data), hashlib.sha256(data).hexdigest()


class FakeGroq:
    calls = 0
    error = None

    async def transcribe(self, path):
        self.calls += 1
        if self.error:
            raise ProviderError(self.error)
        return "Сохрани в проект Игра. Добавить новые кнопки."


class FakeRouter:
    error = False

    async def classify(self, transcript, folders):
        if self.error:
            raise ProviderError("codex_timeout")
        return route_result(next(f for f in folders if f["name"] == "Игра"))


def make_bot(tmp_path, store):
    config = Config("123:secret", frozenset({1}), "secret", tmp_path)
    return Bot(config, store, FakeTelegram(), FakeGroq(), FakeRouter())


def test_complete_pipeline_and_unknown_delivery_not_retried(tmp_path, store):
    async def run():
        game = store.create_folder(1, "Игра", thread=100)
        bot = make_bot(tmp_path, store)
        note = add(store, kind="voice", file_id="voice-id", extension="wav", size=32044, duration=1)
        bot.telegram.fail_delivery = True
        await bot.process(store.claim_note())
        saved = store.note(1, note["id"])
        assert saved["state"] == "ready" and saved["folder_id"] == game["id"]
        assert saved["transcript"].startswith("Сохрани в проект Игра.")
        assert saved["delivery"] == "unknown"
        before = len(bot.telegram.sent)
        await bot.deliver(saved)
        assert len(bot.telegram.sent) == before
        assert bot.archive.audio(saved).exists()
        assert (bot.archive.directory(saved) / "transcript.txt").read_text() == saved["transcript"]
    asyncio.run(run())


def test_asr_failure_preserves_audio_and_llm_failure_does_not_repeat_asr(tmp_path, store):
    async def run():
        store.create_folder(1, "Игра")
        bot = make_bot(tmp_path, store)
        note = add(store, kind="voice", file_id="voice-id", extension="wav", size=32044, duration=1)
        bot.groq.error = "groq_429"
        await bot.process(store.claim_note())
        saved = store.note(1, note["id"])
        assert saved["state"] == "asr_failed" and bot.archive.audio(saved).exists()
        bot.groq.error, bot.router.error = None, True
        store.retry(1, note["id"])
        await bot.process(store.claim_note())
        saved = store.note(1, note["id"])
        assert saved["state"] == "ready" and saved["routing"] == "unavailable"
        asr_calls = bot.groq.calls
        store.retry(1, note["id"])
        bot.router.error = False
        await bot.process(store.claim_note())
        assert bot.groq.calls == asr_calls
        assert store.note(1, note["id"])["routing"] == "explicit"
    asyncio.run(run())


def test_callback_cannot_read_or_delete_another_users_note(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        note = add(store, owner=2, kind="text", transcript="Чужая заметка")
        await bot.handle({"callback_query": {"id": "cb", "from": {"id": 1},
            "message": {"message_id": 10, "chat": {"type": "private", "id": 1}},
            "data": "confirmdelete:" + note["id"]}})
        assert store.note(2, note["id"])["transcript"] == "Чужая заметка"
        assert all("Чужая заметка" not in str(payload) for _, payload in bot.telegram.sent)
    asyncio.run(run())


def test_dispatch_preserves_source_update_when_note_storage_fails(tmp_path, store, monkeypatch):
    async def run():
        bot = make_bot(tmp_path, store)
        update = {"update_id": 3, "message": {"message_id": 7, "from": {"id": 1},
                  "chat": {"id": 1, "type": "private"}, "text": "Нельзя потерять эту идею"}}
        store.ingest([update], {1})
        def disk_failure(*args, **kwargs):
            raise OSError("disk problem")
        monkeypatch.setattr(store, "add_note", disk_failure)
        task = asyncio.create_task(bot.dispatch())
        try:
            await asyncio.sleep(.02)
            saved = store.one("SELECT * FROM updates WHERE id=3")
            assert saved["state"] == "failed"
            assert json.loads(saved["payload"]) == update
            assert store.offset() == 4
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(run())


def test_shutdown_keeps_inflight_update_for_restart(tmp_path, store, monkeypatch):
    async def run():
        bot = make_bot(tmp_path, store)
        update = {"update_id": 3, "message": {"message_id": 7, "from": {"id": 1},
                  "chat": {"id": 1, "type": "private"}, "text": "Идея"}}
        store.ingest([update], {1})
        entered = asyncio.Event()
        async def interrupted(_):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(bot, "handle", interrupted)
        task = asyncio.create_task(bot.dispatch())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.one("SELECT state FROM updates WHERE id=3")["state"] == "processing"
    asyncio.run(run())


def test_config_reads_only_named_bot_from_shared_secrets(tmp_path):
    app = tmp_path / "app.json"
    app.write_text(json.dumps({"allowed_user_ids": [1]}))
    secrets = tmp_path / "shared.json"
    secrets.write_text(json.dumps({"bot_token": "123:WRONG_BOT", "bot_token_your_audio_notes": "456:RIGHT",
                                   "groq_voice": "KEY"}))
    config = Config.load(app, secrets)
    assert config.bot_token == "456:RIGHT" and config.groq_voice == "KEY"
    assert "RIGHT" not in repr(config) and "KEY" not in repr(config)
    app.write_text(json.dumps({"allowed_user_ids": []}))
    with pytest.raises(ValueError, match="allowed_user_ids"):
        Config.load(app, secrets)
