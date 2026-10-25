import asyncio
import json
import zipfile
from dataclasses import replace

import pytest
from test_mvp import FakeGroq, FakeTelegram

from voice_memory_bot.ai_store import AIStore
from voice_memory_bot.assistant import AssistantEngine, CodexAssistant, edge_candidate
from voice_memory_bot.bot import Bot
from voice_memory_bot.config import Config, Limits, LLMConfig
from voice_memory_bot.providers import ProviderError
from voice_memory_bot.store import Store


def step(action="reply", **kwargs):
    return {"action": action, "message": "Ответ", "query": "", "note_id": "", "folder_id": "",
            "title": "", "content": "", "revision": -1, **kwargs}


class Assistant:
    def __init__(self):
        self.calls, self.address_calls = [], []
        self.responses = []

    async def respond(self, payload, model):
        self.calls.append((payload, model))
        result = self.responses.pop(0) if self.responses else step()
        if isinstance(result, Exception):
            raise result
        return result

    async def addressed(self, text, names, model):
        self.address_calls.append(text)
        return text.startswith("Луна,") or text.endswith(", Луна?")


class Router:
    calls = 0

    async def classify(self, transcript, folders):
        self.calls += 1
        return {"title": "Заметка", "explicit": False, "ambiguous": False,
                "folder_id": None, "requested_folder": None, "evidence": None}


@pytest.fixture
def bot(tmp_path):
    store = Store(tmp_path / "db", Limits())
    store.ensure_user(1)
    store.ensure_user(2)
    config = Config("1:token", frozenset({1, 2}), "key", tmp_path, assistant_models=("another-model",))
    app = Bot(config, store, FakeTelegram(), FakeGroq(), Router(), Assistant())
    yield app
    store.close()


async def receive(bot, text="Привет", message_id=101, thread=None, reply=None, voice=False):
    message = {"message_id": message_id, "from": {"id": 1}, "chat": {"id": 1, "type": "private"}}
    if thread:
        message["message_thread_id"] = thread
    if reply:
        message["reply_to_message"] = {"message_id": reply}
    if voice:
        message["voice"] = {"file_id": "voice", "file_size": 32044, "duration": 1}
    else:
        message["text"] = text
    await bot.handle({"message": message})
    note = bot.store.one("SELECT * FROM notes WHERE owner=1 AND message_id=?", (message_id,))
    await bot.process(bot.store.claim_note())
    return bot.store.note(1, note["id"])


def ready_note(bot, text="Старая мысль", owner=1, message_id=1):
    folder = bot.store.ensure_user(owner)
    note, _ = bot.store.add_note(owner, folder, {"message_id": message_id}, "text", transcript=text)
    bot.store.update_note(owner, note["id"], state="ready", title="План", routing="manual", delivery="sent")
    return bot.store.note(owner, note["id"])


def tool(bot, request_id, command):
    index = len(bot.ai_state.steps(1, request_id))
    bot.ai_state.save_command(1, request_id, index, command)
    return bot.ai_state.execute(1, request_id, index)


def test_chat_voice_history_reply_new_conversation_and_model_change(bot):
    async def run():
        folder = bot.store.create_folder(1, "Разговор", 100)
        bot.ai_state.mode(1, folder["id"], "chat")
        bot.store.select(1, folder["id"])
        first = await receive(bot, thread=100, voice=True)
        assert first["entry_type"] == "ai" and bot.router.calls == 0 and bot.groq.calls == 1
        progress = bot.telegram.sent[-1][1]
        assert progress["thread"] == 100 and progress["reply_to"] == first["transcript_message_id"]
        await bot.process_ai(bot.ai_state.claim())
        old = bot.ai_state.request(1, first["id"])["conversation_id"]
        response_id = bot.telegram.sent[-1][1]
        assert bot.telegram.sent[-1][0] == "editMessageText"
        assert response_id["message_id"] == bot.store.one("SELECT message_id FROM ai_progress")["message_id"]
        reply_id = bot.store.one("SELECT message_id FROM ai_links ORDER BY rowid DESC LIMIT 1")["message_id"]
        bot.ai_state.set_settings(1, model="another-model")
        second = await receive(bot, "Продолжи", 102, thread=100, reply=reply_id)
        await bot.process_ai(bot.ai_state.claim())
        assert bot.assistant.calls[-1][1] == "another-model"
        assert bot.assistant.calls[-1][0]["history"][0]["user"] == first["transcript"]
        assert bot.ai_state.request(1, second["id"])["conversation_id"] == old
        await bot.new_ai_conversation(1, 100, folder["id"])
        third = await receive(bot, "Другая тема", 103, thread=100)
        await bot.process_ai(bot.ai_state.claim())
        assert bot.ai_state.request(1, third["id"])["conversation_id"] != old
        assert bot.assistant.calls[-1][0]["history"] == []
        assert len(bot.store.notes(1)) == 2
        assert all(n["ai_generated_from"] for n in bot.store.notes(1))
        assert len(bot.ai_state.history(1, old)) == 2
    asyncio.run(run())


def test_name_toggle_edges_false_mention_and_nonsticky_reply(bot):
    async def run():
        note = await receive(bot, "Луна, помоги", 101)
        assert note["entry_type"] == "note" and not bot.assistant.address_calls
        bot.ai_state.set_settings(1, wake_enabled=True)
        for mid, text, expected in [(102, "Луна, помоги", "ai"), (103, "Что думаешь, Луна?", "ai"),
                                    (104, "Сегодня красивая луна", "note"), (105, "Обычная запись", "note")]:
            assert (await receive(bot, text, mid))["entry_type"] == expected
        assert "Обычная запись" not in bot.assistant.address_calls
        await bot.process_ai(bot.ai_state.claim())
        reply_id = bot.store.one("SELECT message_id FROM ai_links ORDER BY rowid DESC LIMIT 1")["message_id"]
        bot.ai_state.set_settings(1, wake_enabled=False)
        reply = await receive(bot, "Спасибо, продолжи", 106, reply=reply_id)
        plain = await receive(bot, "Заметка отдельно", 107)
        assert reply["entry_type"] == "ai" and plain["entry_type"] == "note"
    asyncio.run(run())


def test_button_waits_for_instruction_and_keeps_selected_note_context(bot):
    async def run():
        note = await receive(bot, voice=True)
        original = bot.store.note(1, note['id'])
        await bot.callback(1, None, note['transcript_message_id'], f"aiask:{note['id']}")
        assert bot.ai_state.claim() is None
        assert bot.store.note(1, note['id']) == original
        assert 'Отправь инструкцию' in bot.telegram.sent[-1][1]['text']
        first = await receive(bot, 'Сделай краткое резюме', 201)
        request = bot.ai_state.claim()
        assert request['note_id'] == first['id']
        await bot.process_ai(request)
        payload = bot.assistant.calls[-1][0]
        assert payload['request'] == 'Сделай краткое резюме'
        assert payload['selected_note']['id'] == original['id']
        assert payload['selected_note']['content'] == original['transcript']
        bot.init_ai(bot.assistant)
        await bot.callback(1, None, 500, f"aicontinue:{first['id']}")
        await receive(bot, 'Теперь переведи на английский', 202)
        continuation = bot.ai_state.claim()
        assert continuation['conversation_id'] == request['conversation_id']
        await bot.process_ai(continuation)
        assert bot.assistant.calls[-1][0]['selected_note'] == payload['selected_note']
        assert bot.assistant.calls[-1][0]['history'][0]['user'] == 'Сделай краткое резюме'
        assert bot.store.note(1, note['id']) == original
        assert bot.groq.calls == 1
        await bot.callback(1, None, 500, f"aiend:{request['conversation_id']}")
        plain = await receive(bot, 'Обычная заметка', 203)
        assert plain['entry_type'] == 'note' and bot.ai_state.claim() is None
        bot.cleanup.begin(1)
        assert bot.store.all('SELECT * FROM ai_note_context WHERE owner=1') == []
    asyncio.run(run())


def test_button_during_address_detection_uses_the_same_note(bot):
    async def run():
        entered, finish = asyncio.Event(), asyncio.Event()
        async def addressed(*args):
            entered.set()
            await finish.wait()
            return False
        bot.assistant.addressed = addressed
        bot.ai_state.set_settings(1, wake_enabled=True, names=["Луна"])
        task = asyncio.create_task(receive(bot, "Луна, объясни это"))
        await asyncio.wait_for(entered.wait(), 2)
        note = bot.store.notes(1)[0]
        await bot.callback(1, None, note["transcript_message_id"], f"aiask:{note['id']}")
        finish.set()
        processed = await task
        assert processed["routing"] == "manual" and processed["state"] == "ready"
        assert bot.ai_state.claim() is None
    asyncio.run(run())


def test_archive_tools_preserve_original_support_search_undo_and_isolate_users(bot, tmp_path):
    request = ready_note(bot, "Измени план", message_id=10)
    bot.ai_state.enqueue(1, request["id"], "button")
    note = ready_note(bot)
    foreign = ready_note(bot, "Секрет другого пользователя", owner=2)
    assert tool(bot, request["id"], step("read_note", note_id=foreign["id"]))["error"]
    assert tool(bot, request["id"], step("update_note", note_id=note["id"], title="Новый план",
                                       content="Попытка без чтения", revision=note["revision"]))["error"]
    current = tool(bot, request["id"], step("read_note", note_id=note["id"]))
    # Changing the folder's default input mode must not make its existing notes uneditable.
    bot.ai_state.mode(1, note["folder_id"], "chat")
    result = tool(bot, request["id"], step("update_note", note_id=note["id"], revision=current["revision"],
                                         title="План встречи", content="ВСТРЕЧА С ЁЖИКОМ"))
    assert result["ok"]
    edited = bot.store.note(1, note["id"])
    assert edited["transcript"] == "Старая мысль" and edited["content"] == "ВСТРЕЧА С ЁЖИКОМ"
    assert bot.store.notes(1, query="встреча ежик")[0]["id"] == note["id"]
    bot.flush_ai_archive()
    bot.ai_state.update_request(1, request["id"], response="План обновлён", state="ready", model=bot.config.llm.model)
    paths = bot.archive.export(bot.store.export_notes(1), bot.store.folders(1), tmp_path / "export", False,
                               45000000, text_format="markdown", settings=bot.store.export_preferences(1))
    with zipfile.ZipFile(paths[0]) as zipped:
        manifest = json.loads(zipped.read("manifest-001.json"))
        exported = [n for f in manifest["files"] if f["kind"] == "history" for n in f["notes"]]
        original = next(n for n in exported if n["id"] == note["id"])
        markdown = zipped.read(next(n for n in zipped.namelist() if n.endswith("history.md"))).decode()
        assert original["transcript"] == "Старая мысль" and "ВСТРЕЧА С ЁЖИКОМ" in markdown
        assert original["revisions"][0]["title"] == "План" and "План обновлён" in markdown
        assert "Секрет другого пользователя" not in markdown
        assert manifest["settings"]["ai"]["notes_access"] == "write"
    restored = bot.ai_state.undo(1, note["id"], result["revision"])
    assert restored["content"] is None and restored["title"] == "План"
    with pytest.raises(ValueError):
        bot.ai_state.undo(1, note["id"], result["revision"])
    with pytest.raises(ValueError):
        bot.ai_state.undo(2, note["id"], restored["revision"])


def test_access_revocation_conflict_and_creation_limits(bot):
    request = ready_note(bot, "Создай заметку", message_id=10)
    bot.ai_state.enqueue(1, request["id"], "button")
    note = ready_note(bot)
    current = tool(bot, request["id"], step("read_note", note_id=note["id"]))
    other_folder = bot.store.create_folder(1, "Работа")
    bot.store.update_note(1, note["id"], folder_id=other_folder["id"])
    assert tool(bot, request["id"], step("update_note", note_id=note["id"], revision=current["revision"],
                                        title="План", content="Замена"))["error"]
    bot.ai_state.set_settings(1, notes_access="read")
    create = step("create_note", folder_id=other_folder["id"], title="Новый", content="Текст")
    assert tool(bot, request["id"], create)["error"]
    bot.ai_state.set_settings(1, notes_access="none")
    assert tool(bot, request["id"], step("search_notes"))["error"]
    bot.ai_state.set_settings(1, notes_access="write")
    foreign_create = {**create, "folder_id": bot.store.ensure_user(2)}
    assert tool(bot, request["id"], foreign_create)["error"]
    created = tool(bot, request["id"], create)
    assert created["ok"] and bot.store.note(1, created["id"])["folder_id"] == other_folder["id"]
    bot.store.limits = replace(bot.store.limits, max_notes_per_user=1)
    assert tool(bot, request["id"], create)["error"]


def test_restart_replays_receipts_without_duplicate_mutation_or_unknown_send(bot, tmp_path):
    async def run():
        request = ready_note(bot, "Создай", message_id=10)
        bot.ai_state.enqueue(1, request["id"], "button")
        command = step("create_note", folder_id=bot.store.ensure_user(1), title="Созданная", content="Текст")
        bot.ai_state.save_command(1, request["id"], 0, command)
        result = bot.ai_state.execute(1, request["id"], 0)
        bot.ai_state.update_request(1, request["id"], state="running")
        reopened = Store(tmp_path / "db", Limits())
        try:
            state = AIStore(reopened)
            provider = Assistant()
            engine = AssistantEngine(state, provider, bot.config.llm.model, bot.ai_models)
            await engine.run(state.claim())
            assert len(reopened.notes(1)) == 2
            assert reopened.note(1, result["id"])["transcript"] == "Текст"
            assert provider.calls[0][0]["steps"][0]["result"]["id"] == result["id"]
        finally:
            reopened.close()
        bot.telegram.fail_delivery = True
        await bot.deliver_ai(bot.ai_state.request(1, request["id"]))
        calls = len(bot.telegram.sent)
        bot.telegram.fail_delivery = False
        await bot.deliver_ai(bot.ai_state.request(1, request["id"]))
        assert len(bot.telegram.sent) == calls
        assert bot.ai_state.request(1, request["id"])["delivery"] == "unknown"
        await bot.deliver_ai(bot.ai_state.request(1, request["id"]), force=True)
        assert bot.ai_state.request(1, request["id"])["delivery"] == "sent"
    asyncio.run(run())


def test_provider_failure_after_write_reports_receipt_and_retry_does_not_write_twice(bot):
    async def run():
        note = await receive(bot, "Создай план")
        bot.ai_state.enqueue(1, note["id"], "button")
        bot.assistant.responses = [step("create_note", folder_id=bot.store.ensure_user(1), title="План", content="План"),
                                   ProviderError("ai_generation_failed")]
        await bot.process_ai(bot.ai_state.claim())
        assert bot.ai_state.request(1, note["id"])["state"] == "failed"
        assert "Создано: План" in bot.telegram.sent[-1][1]["text"]
        await bot.callback(1, None, 99, f"airetry:{note['id']}")
        await bot.process_ai(bot.ai_state.claim())
        assert len(bot.store.notes(1)) == 2 and bot.ai_state.request(1, note["id"])["state"] == "ready"
        assert sum(n["title"] == "План" for n in bot.store.notes(1)) == 1
    asyncio.run(run())


def test_folder_mode_snapshot_and_reply_survive_restart_before_asr(bot, tmp_path):
    async def run():
        folder = bot.store.create_folder(1, "ИИ", thread=100)
        bot.ai_state.mode(1, folder["id"], "chat")
        bot.store.select(1, folder["id"])
        note, _ = bot.store.add_note(1, folder["id"], {"message_id": 10, "message_thread_id": 100},
                                      "text", transcript="Привет")
        bot.ai_state.mode(1, folder["id"], "notes")
        await bot.process(bot.store.claim_note())
        assert bot.store.note(1, note["id"])["entry_type"] == "ai"
        assert bot.router.calls == 0
    asyncio.run(run())


def test_real_address_adapter_rejects_invented_evidence_and_middle_names():
    class Provider(CodexAssistant):
        result = {"addressed": True, "evidence": "Луна, помоги"}

        async def structured(self, *args):
            return self.result
    async def run():
        provider = Provider(LLMConfig())
        assert await provider.addressed("Луна, помоги составить план", ["Луна"], "model")
        assert not await provider.addressed("Сегодня красивая луна", ["Луна"], "model")
        assert not edge_candidate("слово " * 100 + "Луна " + "слово " * 100, ["Луна"])
        provider.result = {"addressed": "true", "evidence": "Луна"}
        with pytest.raises(ProviderError):
            await provider.addressed("Луна, привет", ["Луна"], "model")
    asyncio.run(run())


def test_settings_controls_and_ownership(bot):
    async def run():
        await bot.ai_settings(1, None)
        text = json.dumps(bot.bottom_state(1), ensure_ascii=False)
        assert "Изменить имена" not in text
        await bot.callback(1, None, 1, "aiwake:1")
        assert "Изменить имена" in json.dumps(bot.bottom_state(1), ensure_ascii=False)
        await bot.callback(1, None, 1, "ainames")
        await bot.input_action(1, None, "Помощник, Luna")
        assert bot.ai_state.settings(1)["names"] == ["помощник", "luna"]
        await bot.callback(1, None, 1, "aimodel:1")
        assert bot.ai_engine.model(1) == "another-model"
        foreign = ready_note(bot, owner=2)
        with pytest.raises(ValueError):
            await bot.callback(1, None, 1, f"aiask:{foreign['id']}")
    asyncio.run(run())


def test_revoke_write_access_while_model_is_generating(bot):
    async def run():
        note = await receive(bot, "Создай заметку")
        bot.ai_state.enqueue(1, note["id"], "button")
        calls = []
        async def respond(payload, model):
            calls.append(payload)
            if len(calls) == 1:
                bot.ai_state.set_settings(1, notes_access="none")
                return step("create_note", folder_id=bot.store.ensure_user(1), title="Нельзя", content="Нельзя")
            assert payload["steps"][0]["result"]["error"]
            assert payload["notes_access"] == "none" and not payload["folders"]
            return step(message="Доступ выключен")
        bot.assistant.respond = respond
        await bot.process_ai(bot.ai_state.claim())
        assert [n["transcript"] for n in bot.store.notes(1)] == ["Доступ выключен"]
    asyncio.run(run())


def test_ai_queue_rejects_new_input_before_archiving_and_keeps_full_context(bot):
    async def run():
        folder = bot.store.create_folder(1, "ИИ", 100)
        bot.ai_state.mode(1, folder["id"], "chat")
        bot.store.select(1, folder["id"])
        bot.store.limits = replace(bot.store.limits, max_pending_per_user=1)
        first = await receive(bot, "Привет", thread=100)
        await bot.handle({"message": {"message_id": 102, "text": "Ещё", "message_thread_id": 100,
                                     "from": {"id": 1}, "chat": {"id": 1, "type": "private"}}})
        assert bot.store.one("SELECT count(*) AS n FROM notes WHERE owner=1")["n"] == 1
        assert "Очередь ИИ заполнена" in bot.telegram.sent[-1][1]["text"]
        await bot.process_ai(bot.ai_state.claim())
        bot.ai_state.update_request(1, first["id"], response="Очень длинная история " * 3000)
        second = await receive(bot, "Продолжим", message_id=103, thread=100)
        payload = bot.ai_engine.payload(bot.ai_state.request(1, second["id"]), [])
        assert not payload["history_truncated"]
        assert payload["history"][0]["assistant"] == "Очень длинная история " * 3000
        assert len(bot.ai_state.history(1, bot.ai_state.request(1, first["id"])["conversation_id"])) == 1
    asyncio.run(run())


def test_selected_note_scope_isolated_and_cleared_by_folder_switch(bot):
    async def run():
        first = await receive(bot, 'Исходник A', 301)
        second = await receive(bot, 'Исходник B', 302)
        await bot.callback(1, None, first['transcript_message_id'], f"aiask:{first['id']}")
        instruction = await receive(bot, 'Сократи', 303)
        request = bot.ai_state.claim()
        denied = tool(bot, instruction['id'], step('read_note', note_id=second['id']))
        assert 'error' in denied
        bot.ai_state.set_settings(1, notes_access='none')
        payload = bot.ai_engine.payload(request, [])
        assert payload['request'] == 'Сократи'
        assert payload['selected_note']['content'] == 'Исходник A'
        await bot.process_ai(request)
        await bot.show_notes(1, None, first['folder_id'])
        assert bot.store.action(1) is None
        plain = await receive(bot, 'Обычная заметка', 304)
        assert plain['entry_type'] == 'note'
        await bot.callback(1, None, second['transcript_message_id'], f"aiask:{second['id']}")
        await receive(bot, 'Переведи', 305)
        next_request = bot.ai_state.claim()
        payload = bot.ai_engine.payload(next_request, [])
        assert payload['selected_note']['content'] == 'Исходник B'
        assert payload['history'] == []
    asyncio.run(run())


@pytest.mark.parametrize("voice", [False, True])
def test_selected_photo_is_attached_to_instructions_and_followups(bot, voice):
    from test_photos import PHOTO, receive_photo

    async def run():
        bot.store.set_message_buttons(1, True)
        bot.store.create_folder(1, "Фото", thread=100)
        async def download(file_id, path, maximum):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PHOTO)
            return len(PHOTO), "photo-checksum"
        bot.telegram.download = download
        await receive_photo(bot)
        note = bot.store.notes(1)[0]
        conversation = bot.ai_state.start_note_conversation(1, note['id'])
        assert bot.ai_state.note_context(1, conversation)['note_id'] == note['id']
        await bot.process(bot.store.claim_note())
        note = bot.store.note(1, note['id'])
        original = dict(note)
        await bot.callback(1, 100, note['transcript_message_id'], f"options:{note['id']}")
        rows = bot.telegram.sent[-1][1]['reply_markup']['inline_keyboard']
        assert any(k['callback_data'] == f"aiask:{note['id']}" for row in rows for k in row)
        await bot.callback(1, 100, note['transcript_message_id'], f"aiask:{note['id']}")
        assert bot.ai_state.claim() is None
        bot.ai_state.set_settings(1, notes_access="none")
        calls = []
        async def respond(payload, model, images=()):
            calls.append((payload, list(images)))
            return step(message="Распознанный текст")
        bot.assistant.respond = respond
        # Exercise voice ingestion with the fake ASR provider.
        bot.telegram.download = FakeTelegram().download
        instruction = await receive(bot, "Распознай текст; сравни с https://example.com", 201, thread=100, voice=voice)
        transcript_sends = [p for m, p in bot.telegram.sent
                            if m in {'sendMessage', 'editMessageText'} and p.get('text') == instruction['transcript']]
        assert transcript_sends
        assert not (transcript_sends[-1].get('keyboard') or
                    transcript_sends[-1].get('reply_markup') or {}).get('inline_keyboard')
        request = bot.ai_state.claim()
        await bot.process_ai(request)
        assert bot.telegram.sent[-1][0] == 'editMessageText'
        assert bot.telegram.sent[-1][1]['reply_markup']['inline_keyboard'] == []
        before_refresh = len(bot.telegram.sent)
        with bot.store.db:
            bot.store.db.execute('UPDATE telegram_messages SET sent_at=1')
        await bot.refresh_due_views()
        assert len(bot.telegram.sent) == before_refresh
        await bot.show_note(1, note['id'], 100)
        assert bot.telegram.sent[-1][0] == 'sendPhoto'
        assert bot.telegram.sent[-1][1]['photo'] == note['file_id']
        assert not any(m == 'deleteMessage' and p['message_id'] == note['result_message_id']
                       for m, p in bot.telegram.sent)
        assert calls[-1][0]['selected_note']['kind'] == 'photo'
        assert calls[-1][1] == [bot.archive.audio(note)]
        assert bot.ai_state.request(1, request['note_id'])['state'] == 'ready'
        bot.init_ai(bot.assistant)
        assert bot.store.action(1).startswith("ai_note:")
        await bot.callback(1, 100, 900, f"aicontinue:{request['note_id']}")
        followup = await receive(bot, "Теперь переведи", 202, thread=100)
        await bot.process_ai(bot.ai_state.claim())
        assert calls[-1][1] == [bot.archive.audio(note)]
        assert bot.archive.audio(note).read_bytes() == PHOTO
        assert bot.store.note(1, note['id']) == original
        bot.archive.audio(note).unlink()
        await bot.callback(1, 100, 901, f"aicontinue:{followup['id']}")
        missing = await receive(bot, "Ещё вопрос", 203, thread=100)
        await bot.process_ai(bot.ai_state.claim())
        assert len(calls) == 2  # Never pretend to analyze a missing image.
        assert bot.ai_state.request(1, missing['id'])['state'] == 'failed'
    asyncio.run(run())


def test_reply_continuation_keeps_more_than_twelve_turns(bot):
    async def run():
        original = await receive(bot, "Исходная заметка", 400)
        await bot.callback(1, None, 500, f"aiask:{original['id']}")
        await receive(bot, "Первый вопрос", 401)
        await bot.process_ai(bot.ai_state.claim())
        for index in range(14):
            sent = bot.telegram.sent[-1][1]
            keyboard = sent.get('reply_markup') or sent.get('keyboard') or {}
            assert not any(b['callback_data'].startswith('aicontinue:')
                           for row in keyboard.get('inline_keyboard', []) for b in row)
            bot.init_ai(bot.assistant)
            await receive(bot, f"Уточнение {index}", 402 + index)
            assert bot.store.action(1).startswith("ai_note:")
            await bot.process_ai(bot.ai_state.claim())
        payload = bot.assistant.calls[-1][0]
        assert len(payload['history']) == 14
        assert payload['history'][0]['user'] == 'Первый вопрос'
        assert payload['history'][0]['assistant'] == 'Ответ'
        assert payload['selected_note']['content'] == 'Исходная заметка'
        await bot.bottom_input(1, None, 'Отмена', 499)
        plain = await receive(bot, 'Новая заметка', 500)
        assert plain['entry_type'] == 'note'
        assert bot.ai_state.request(1, plain['id']) is None
    asyncio.run(run())


def test_cancel_continuation_deletes_prompt_and_restores_note_input(bot):
    async def run():
        note = await receive(bot, "Исходная заметка")
        await bot.callback(1, None, 500, f"aiask:{note['id']}")
        question = await receive(bot, "Вопрос", 201)
        await bot.process_ai(bot.ai_state.claim())
        request = bot.ai_state.request(1, question['id'])
        await bot.callback(1, None, 500, f"aicontinue:{question['id']}")
        prompt_id = bot.telegram.message_sequence
        controls = next(p['keyboard']['keyboard'] for m, p in reversed(bot.telegram.sent)
                        if m == 'sendMessage' and p.get('keyboard'))
        assert controls == [[{'text': 'Отмена'}]]
        cancel_action = bot.bottom_state(1)['actions']['Отмена']
        assert bot.store.action(1) == f"ai_note:{request['conversation_id']}"
        before = len(bot.telegram.sent)
        await bot.callback(1, None, prompt_id, cancel_action)
        assert bot.store.action(1) is None
        events = bot.telegram.sent[before:]
        assert next(i for i, (m, _) in enumerate(events) if m == 'sendMessage') < next(
            i for i, (m, p) in enumerate(events) if m == 'deleteMessage' and p['message_id'] == prompt_id)
        plain = await receive(bot, "Обычная заметка", 202)
        assert plain['entry_type'] == 'note'
        assert bot.ai_state.claim() is None
        # An old cancel button must not clear another pending action.
        bot.store.set_action(1, 'search')
        await bot.callback(1, None, prompt_id, cancel_action)
        assert bot.store.action(1) == 'search'
    asyncio.run(run())
