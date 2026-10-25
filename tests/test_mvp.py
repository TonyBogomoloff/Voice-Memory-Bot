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


def test_search_and_listing_share_pagination_and_unlimited_contract(store):
    first = add(store, message=1, kind="text", transcript="Ёжик один")
    assert [n["id"] for n in store.notes(1, query="ежик", limit=None)] == [first["id"]]
    for message in (2, 3):
        add(store, message=message, kind="text", transcript="Ёжик ещё")
    for offset, limit, count in [(0, None, 3), (0, -1, 3), (1, None, 2), (1, 1, 1), (0, 0, 0)]:
        listed = store.notes(1, offset=offset, limit=limit)
        searched = store.notes(1, query="ЕЖИК", offset=offset, limit=limit)
        assert len(searched) == count
        assert [n["id"] for n in searched] == [n["id"] for n in listed]
    for kwargs in ({"offset": -1}, {"limit": -2}, {"limit": True}, {"offset": 1.5}):
        with pytest.raises(ValueError):
            store.notes(1, **kwargs)


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


def test_telegram_service_events_do_not_create_custom_folders(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        base = {"from": {"id": 1}, "chat": {"id": 1, "type": "private"}, "message_thread_id": 99}
        await bot.handle({"message": {**base, "message_id": 1, "forum_topic_created": {
            "name": "Новая заметка", "is_name_implicit": True}}})
        await bot.handle({"message": {**base, "message_id": 2, "text": "Идея для будущего"}})
        assert store.folder_for_thread(1, 99) is None
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
        assert zipped.read(next(n for n in names if n.endswith(".ogg"))) == b"unchanged audio"
        manifest = zipped.read("manifest-001.json")
        history = next(f for f in json.loads(manifest)["files"] if f["kind"] == "history")
        assert history["notes"][0]["transcript"] == note["transcript"]
        assert b"not-for-export" not in manifest
    audio.write_bytes(b"modified  audio")
    with pytest.raises(ValueError, match="checksum"):
        archive.export([note], store.folders(1), tmp_path / "bad-export", True, 45000000)


def test_export_split_text_only_and_delete(tmp_path, store):
    notes = [add(store, message=i, kind="text", transcript="x" * 1000) for i in range(3)]
    for i, note in enumerate(notes):
        folder = store.create_folder(1, f"Проект {i}")
        store.update_note(1, note["id"], folder_id=folder["id"])
    notes = store.export_notes(1)
    archive = Archive(tmp_path / "files")
    paths = archive.export(notes, store.folders(1), tmp_path / "export", False, 5200, text_format="markdown")
    assert len(paths) == 3
    assert all(p.stat().st_size <= 5200 for p in paths)
    exported, manifests = [], []
    for path in paths:
        with zipfile.ZipFile(path) as zipped:
            name = next(n for n in zipped.namelist() if n.startswith("manifest-"))
            manifests.append(name)
            exported.extend(n["id"] for f in json.loads(zipped.read(name))["files"] for n in f["notes"])
    assert len(set(manifests)) == len(paths)
    assert sorted(exported) == sorted(n["id"] for n in notes)
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
        self.message_sequence = 0

    async def call(self, method, **kwargs):
        self.sent.append((method, kwargs))
        if method == "createForumTopic":
            return {"message_thread_id": 123}
        if method.startswith("editMessage"):
            return {"message_id": kwargs["message_id"]}
        if self.fail_delivery and method.startswith("send"):
            raise ProviderError("telegram_transport_unknown")
        self.message_sequence += 1
        result = {"message_id": self.message_sequence}
        if getattr(self, "on_message", None) and method.startswith("send"):
            self.on_message(kwargs["chat_id"], result["message_id"],
                            kwargs.get("message_thread_id", kwargs.get("thread")))
        return result

    async def text(self, owner, text, thread=None, keyboard=None, reply_to=None):
        return await self.call("sendMessage", chat_id=owner, text=text, thread=thread, keyboard=keyboard,
                               reply_to=reply_to)

    async def photo(self, owner, path, thread=None, caption=None, keyboard=None, reply_to=None, edit_id=None):
        result = await self.call("editMessageMedia" if edit_id else "sendPhoto", chat_id=owner,
            path=str(path), image=path.read_bytes(), message_id=edit_id, message_thread_id=thread,
            caption=caption, reply_markup=keyboard, reply_to=reply_to)
        return {**result, "photo": [{"file_id": "generated-photo", "width": 1024, "height": 1024}]}

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

    async def transcribe(self, path, *, prompt=None):
        self.calls += 1
        if self.error:
            raise ProviderError(self.error)
        return "Сохрани в проект Игра. Добавить новые кнопки."


class FakeRouter:
    error = False

    async def folder_name(self, transcript):
        raise ProviderError("fake_folder_name_disabled")

    async def classify(self, transcript, folders):
        if self.error:
            raise ProviderError("codex_timeout")
        return route_result(next(f for f in folders if f["name"] == "Игра"))


def make_bot(tmp_path, store):
    config = Config("123:secret", frozenset({1}), "secret", tmp_path)
    app = Bot(config, store, FakeTelegram(), FakeGroq(), FakeRouter())
    app.folder_namer = app.router
    return app


async def receive_voice(bot, message_id=10, thread=100):
    await bot.handle({"message": {"message_id": message_id, "from": {"id": 1},
        "chat": {"id": 1, "type": "private"}, "message_thread_id": thread,
        "voice": {"file_id": "voice-id", "file_size": 32044, "duration": 1}}})


@pytest.mark.parametrize("thread", [None, 100])
def test_voice_transcript_is_delivered_without_calling_folder_classifier(tmp_path, store, thread):
    async def run():
        store.set_message_buttons(1, True)
        game = store.create_folder(1, "Игра", thread=100)
        store.select(1, game["id"])
        bot = make_bot(tmp_path, store)
        async def unused_router(*args):
            raise AssertionError("A folder classifier must not run for an ordinary note")
        bot.router.classify = unused_router
        await receive_voice(bot, thread=thread)
        status_id = store.notes(1)[0]["status_message_id"]
        await bot.process(store.claim_note())
        saved = store.notes(1)[0]
        assert saved["folder_id"] == game["id"] and saved["routing"] == "manual"
        assert saved["state"] == "ready" and saved["delivery"] == "sent"
        assert saved["status_message_id"] is None
        assert [m for m, _ in bot.telegram.sent] == ["sendMessage", "editMessageText"]
        # Telegram preserves reply linkage when editing the status into a transcript.
        assert bot.telegram.sent[0][1].get("reply_to") is None
        payload = bot.telegram.sent[-1][1]
        assert payload["text"] == saved["transcript"]
        assert payload["message_id"] == status_id
        assert saved["transcript_message_id"] == payload["message_id"]
        assert payload["reply_markup"]["inline_keyboard"][0][-1]["text"] == "ИИ"
    asyncio.run(run())

@pytest.mark.parametrize("thread", [None, 100])
def test_long_transcript_is_sent_as_complete_text_messages(tmp_path, store, thread):
    async def run():
        store.set_message_buttons(1, True)
        store.create_folder(1, "Игра", thread=100)
        bot = make_bot(tmp_path, store)
        raw = "Сохрани в проект Игра.\n" + "Идея 🚀 <текст> & ещё одна.\n" * 400
        async def transcribe(_):
            return raw
        bot.groq.transcribe = transcribe
        await receive_voice(bot, thread=thread)
        await bot.process(store.claim_note())
        messages = [payload for method, payload in bot.telegram.sent
                    if method in {"sendMessage", "editMessageText"} and payload["text"] != "Конвертирую в текст…"]
        assert len(messages) > 1 and "".join(p["text"] for p in messages) == raw
        assert all(len(p["text"].encode("utf-16-le")) <= 7800 for p in messages)
        assert all(p.get("reply_to") is None for m, p in bot.telegram.sent if m == "sendMessage")
        assert not any(m == "deleteMessage" for m, _ in bot.telegram.sent)
        assert all(p.get("keyboard", p.get("reply_markup")) is None for p in messages[:-1])
        assert messages[-1]["keyboard"]["inline_keyboard"][0][-1]["text"] == "ИИ"
        assert all(method != "sendDocument" for method, _ in bot.telegram.sent)
    asyncio.run(run())


def test_voice_options_move_changes_archive_and_removes_source(tmp_path, store):
    async def run():
        store.set_message_buttons(1, True)
        store.create_folder(1, "Игра", thread=100)
        work = store.create_folder(1, "Работа", thread=200)
        bot = make_bot(tmp_path, store)
        await receive_voice(bot)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        message_id = note["transcript_message_id"]
        bot.telegram.sent.clear()
        await bot.callback(1, 100, message_id, f"options:{note['id']}")
        keys = bot.telegram.sent[-1][1]["reply_markup"]["inline_keyboard"]
        assert keys == [
            [{"text": "Переместить", "callback_data": f"pick:{note['id']}:0"},
            {"text": "Удалить", "callback_data": f"delete:{note['id']}"},
            {"text": "ИИ", "callback_data": f"aiask:{note['id']}"}]]
        await bot.callback(1, 100, message_id, next(b["callback_data"] for row in keys for b in row
                                                     if b["callback_data"].startswith("pick:")))
        state = bot.bottom_state(1)
        assert state["screen"].startswith("replymove:")
        assert "Работа" in state["actions"]
        await bot.bottom_input(1, 100, "Работа", 9000)
        assert store.note(1, note["id"])["folder_id"] == work["id"]
        assert any(method == "deleteMessage" and payload["message_id"] == message_id
                   for method, payload in bot.telegram.sent)
        assert any(method == "sendMessage" and payload["text"] == "Перенесено в «Работа»."
                   for method, payload in bot.telegram.sent)
    asyncio.run(run())


def test_voice_failure_removes_status_and_can_retry(tmp_path, store):
    async def run():
        store.create_folder(1, "Игра", thread=100)
        bot = make_bot(tmp_path, store)
        bot.groq.error = "groq_429"
        await receive_voice(bot)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        assert note["state"] == "asr_failed" and note["status_message_id"] is None
        assert note["transcript_delivery"] == "pending"
        assert "Не удалось конвертировать" in bot.telegram.sent[-1][1]["text"]
        bot.groq.error = None
        await bot.callback(1, 100, note["result_message_id"], f"retry:{note['id']}")
        assert bot.telegram.sent[-1][0] == "editMessageText"
        assert bot.telegram.sent[-1][1]["text"] == "Конвертирую в текст…"
        await bot.process(store.claim_note())
        saved = store.note(1, note["id"])
        assert saved["transcript_delivery"] == "sent" and saved["state"] == "ready"
        assert bot.telegram.sent[-1][1]["text"] == saved["transcript"]
    asyncio.run(run())


def test_uncertain_status_edit_is_not_deleted_or_sent_again(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        call = bot.telegram.call
        async def lost_edit_response(method, **kwargs):
            result = await call(method, **kwargs)
            if method == "editMessageText":
                raise ProviderError("telegram_transport_unknown")
            return result
        bot.telegram.call = lost_edit_response
        await receive_voice(bot)
        await bot.process(store.claim_note())
        note = store.notes(1)[0]
        assert note["transcript_delivery"] == "unknown" and note["status_message_id"] is None
        assert [m for m, _ in bot.telegram.sent] == ["sendMessage", "editMessageText"]
        await bot.clear_status(note)
        await bot.deliver_transcript(note)
        assert len(bot.telegram.sent) == 2
    asyncio.run(run())


@pytest.mark.parametrize("delivery", ["sent", "sending"])
def test_transcript_restart_cleans_status_without_resending(tmp_path, delivery):
    async def run():
        path = tmp_path / "restart.sqlite3"
        store = Store(path, Limits())
        store.ensure_user(1)
        store.create_folder(1, "Игра")
        bot = make_bot(tmp_path, store)
        note = add(store, kind="voice", file_id="voice-id", transcript="Сохрани в проект Игра. Мысль.")
        store.claim_note()
        store.update_note(1, note["id"], transcript_delivery=delivery, status_message_id=20,
                          transcript_message_id=21 if delivery == "sent" else None)
        store.close()
        store = Store(path, Limits())
        try:
            bot = make_bot(tmp_path, store)
            await bot.process(store.claim_note())
            assert bot.telegram.sent == [("deleteMessage", {"chat_id": 1, "message_id": 20})]
            saved = store.note(1, note["id"])
            assert saved["delivery"] == ("unknown" if delivery == "sending" else "sent")
            assert saved["status_message_id"] is None
        finally:
            store.close()
    asyncio.run(run())


def test_transcript_migration_keeps_delivery_history_and_failed_asr_retryable(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    store = Store(path, Limits())
    store.ensure_user(1)
    ready = add(store, message=10, kind="voice", file_id="voice-id", transcript="Сохранённый текст")
    failed = add(store, message=11, kind="voice", file_id="voice-id")
    store.update_note(1, ready["id"], state="ready", delivery="sent")
    store.update_note(1, failed["id"], state="asr_failed", delivery="sent")
    for name in ("status_message_id", "transcript_delivery", "transcript_message_id"):
        store.db.execute(f"ALTER TABLE notes DROP COLUMN {name}")
    store.close()
    store = Store(path, Limits())
    try:
        assert store.note(1, ready["id"])["transcript_delivery"] == "sent"
        assert store.note(1, ready["id"])["transcript"] == "Сохранённый текст"
        assert store.note(1, failed["id"])["transcript_delivery"] == "pending"
        assert store.note(1, failed["id"])["delivery"] == "sent"
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["voice", "text"])
def test_move_does_not_contact_deleted_telegram_topics(tmp_path, store, kind):
    async def run():
        target = store.create_folder(1, "Игра", thread=100)
        bot = make_bot(tmp_path, store)
        if kind == "voice":
            await receive_voice(bot)
            note = store.notes(1)[0]
        else:
            note = add(store, kind=kind, transcript="Длинная идея. " * 700)
        await bot.process(store.claim_note())
        bot.telegram.sent.clear()
        bot.telegram.fail_delivery = True
        await bot.move(1, note["id"], target["id"], None)
        assert store.note(1, note["id"])["folder_id"] == target["id"]
        assert all(method == "deleteMessage" for method, _ in bot.telegram.sent)
    asyncio.run(run())


def test_complete_pipeline_and_unknown_delivery_not_retried(tmp_path, store):
    async def run():
        store.create_folder(1, "Игра", thread=100)
        bot = make_bot(tmp_path, store)
        note = add(store, kind="voice", file_id="voice-id", extension="wav", size=32044, duration=1)
        bot.telegram.fail_delivery = True
        await bot.process(store.claim_note())
        saved = store.note(1, note["id"])
        assert saved["state"] == "ready" and saved["folder_id"] == note["source_folder"]
        assert saved["transcript"].startswith("Сохрани в проект Игра.")
        assert saved["delivery"] == "unknown"
        before = len(bot.telegram.sent)
        await bot.deliver(saved)
        assert len(bot.telegram.sent) == before
        assert bot.archive.audio(saved).exists()
        assert (bot.archive.directory(saved) / "transcript.txt").read_text() == saved["transcript"]
    asyncio.run(run())


def test_asr_failure_preserves_audio_and_retry_needs_no_folder_classifier(tmp_path, store):
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
        assert saved["state"] == "ready" and saved["routing"] == "manual"
        assert saved["folder_id"] == note["source_folder"] and bot.groq.calls == 2
        with pytest.raises(ValueError, match="уже обработана"):
            store.retry(1, note["id"])
        assert bot.groq.calls == 2
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


def test_config_reads_only_named_bot_from_shared_secrets(tmp_path, monkeypatch):
    app = tmp_path / "app.json"
    app.write_text(json.dumps({"allowed_user_ids": [1], "web_url": "retired", "web_port": "unused",
                               "folder_name_model": "naming-model"}))
    monkeypatch.setenv("VOICE_MEMORY_WEB_URL", "retired")
    monkeypatch.setenv("VOICE_MEMORY_WEB_PORT", "unused")
    secrets = tmp_path / "shared.json"
    secrets.write_text(json.dumps({"bot_token": "123:WRONG_BOT", "bot_token_your_audio_notes": "456:RIGHT",
                                   "groq_voice": "KEY"}))
    config = Config.load(app, secrets)
    assert config.bot_token == "456:RIGHT" and config.groq_voice == "KEY"
    assert config.folder_name_model == "naming-model"
    assert "RIGHT" not in repr(config) and "KEY" not in repr(config)
    app.write_text(json.dumps({"allowed_user_ids": []}))
    with pytest.raises(ValueError, match="allowed_user_ids"):
        Config.load(app, secrets)


@pytest.mark.parametrize("fallback", ["missing_status", "edit_refused"])
def test_voice_transcript_fallback_is_standalone_with_options(tmp_path, store, fallback):
    async def run():
        store.set_message_buttons(1, True)
        bot = make_bot(tmp_path, store)
        await receive_voice(bot, thread=None)
        note = store.notes(1)[0]
        if fallback == 'missing_status':
            store.update_note(1, note['id'], status_message_id=None)
        else:
            original_call = bot.telegram.call

            async def refuse_edit(method, **payload):
                if method == 'editMessageText':
                    raise ProviderError('telegram_400')
                return await original_call(method, **payload)

            bot.telegram.call = refuse_edit
        await bot.process(store.claim_note())
        sent = [p for m, p in bot.telegram.sent if m == 'sendMessage']
        assert all(p.get('reply_to') is None for p in sent)
        assert sent[-1]['text'] == store.note(1, note['id'])['transcript']
        assert sent[-1]['keyboard']['inline_keyboard'][0][0]['text'] == 'Переместить'
    asyncio.run(run())
