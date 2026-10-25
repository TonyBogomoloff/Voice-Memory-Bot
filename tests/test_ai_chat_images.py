import asyncio
import json
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest
from test_ai import bot as bot
from test_ai import receive, step
from test_ai_images import image_assistant, photo
from test_ai_images import image_cli as image_cli
from test_photos import PHOTO, receive_photo

from voice_memory_bot.providers import ProviderError, Telegram, TelegramError


@pytest.mark.parametrize("edit", [False, True])
def test_chat_image_replaces_progress_and_keeps_source_and_receipts(bot, image_cli, edit):
    async def run():
        image_assistant(bot)
        source = photo(bot) if edit else None
        before = bot.archive.audio(source).read_bytes() if source else None
        if source:
            await bot.callback(1, None, 700, f"aiask:{source['id']}")
        command = await receive(bot, "Перекрась в оранжевый" if edit else "Нарисуй круг",
                                reply=None if edit else 777)
        progress = bot.store.one("SELECT message_id FROM ai_progress")["message_id"]
        bot.assistant.responses = ([step("read_note", note_id=source["id"])] if edit else []) + [
            step("edit_image" if edit else "create_image", note_id=source["id"] if edit else "",
                 content="Оранжевый круг", title="Круг")]
        await bot.process_ai(bot.ai_state.claim())
        request = bot.ai_state.request(1, command["id"])
        assert request["state"] == "ready" and request["delivery"] == "sent"
        assert bot.assistant.calls[0][0]["image_generation"]
        method, sent = bot.telegram.sent[-1]
        assert method == "editMessageMedia" and sent["message_id"] == progress
        assert "Картинка готова" in sent["caption"] and sent["image"].startswith(b"\x89PNG")
        created = bot.store.note(1, bot.ai_engine.receipts(1, command["id"])[0]["id"])
        assert created["file_id"] == "generated-photo"
        assert bot.ai_state.message_note(1, progress)["id"] == created["id"]
        assert bot.ai_state.reply_conversation(1, progress) == request["conversation_id"]
        if source:
            assert bot.archive.audio(source).read_bytes() == before
            assert "--image" in image_cli[0][0]
        count = len(bot.telegram.sent)
        bot.init_ai(bot.assistant)
        await bot.deliver_ai(request)
        assert len(bot.telegram.sent) == count and len(image_cli) == 1
        await bot.show_note(1, created["id"], None)
        assert bot.telegram.sent[-1][1]["photo"] == "generated-photo"
        # A follow-up can read and edit the generated image within the same conversation.
        followup = await receive(bot, "Теперь синий", message_id=102, reply=progress)
        bot.assistant.responses = [step("read_note", note_id=created["id"]),
            step("edit_image", note_id=created["id"], content="Сделай синим")]
        await bot.process_ai(bot.ai_state.claim())
        assert bot.ai_state.request(1, followup["id"])["state"] == "ready"
        assert len(bot.ai_engine.receipts(1, followup["id"])) == 1 and len(image_cli) == 2
    asyncio.run(run())


@pytest.mark.parametrize("failure", [False, True])
def test_instruction_can_arrive_while_selected_photo_is_downloading(bot, failure):
    async def run():
        started, finish = asyncio.Event(), asyncio.Event()
        async def download(file_id, path, maximum):
            started.set()
            await finish.wait()
            if failure:
                raise ProviderError("download_failed")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PHOTO)
            return len(PHOTO), "checksum"
        bot.telegram.download = download
        await receive_photo(bot)
        source = bot.store.notes(1)[0]
        task = asyncio.create_task(bot.process(bot.store.claim_note()))
        await started.wait()
        try:
            await bot.callback(1, None, None, f"aiask:{source['id']}")
            assert "Отправь инструкцию для ИИ" in bot.telegram.sent[-1][1]["text"]
            instruction = await receive(bot, "Что на фото?", message_id=201)
            assert bot.telegram.sent[-1][1]["text"] == "Думаю…"
            assert bot.ai_state.claim() is None and not bot.assistant.calls
            bot.init_ai(bot.assistant)
            assert bot.ai_state.claim() is None  # Dependency is durable, not an in-memory delay.
        finally:
            finish.set()
            await task
        calls = []
        async def respond(payload, model, images=()):
            calls.append(list(images))
            return step(message="Вижу фото")
        bot.assistant.respond = respond
        request = bot.ai_state.claim()
        assert request["note_id"] == instruction["id"]
        await bot.process_ai(request)
        assert bot.ai_state.request(1, instruction["id"])["state"] == ("failed" if failure else "ready")
        assert calls == ([] if failure else [[bot.archive.audio(source)]])
        assert bot.telegram.sent[-1][0] == "editMessageText"
        assert "Думаю" not in bot.telegram.sent[-1][1]["text"]
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["message to edit not found", "message is not modified", "network"])
def test_image_delivery_falls_back_only_on_confirmed_missing_progress(bot, image_cli, reason):
    async def run():
        image_assistant(bot)
        note = await receive(bot, "Нарисуй круг", reply=777)
        bot.assistant.responses = [step("create_image", content="Круг")]
        upload, calls = bot.telegram.photo, []
        async def uncertain(*args, **kwargs):
            calls.append(kwargs)
            if kwargs.get("edit_id"):
                raise ProviderError("network") if reason == "network" else TelegramError(400, "editMessageMedia", reason)
            return await upload(*args, **kwargs)
        bot.telegram.photo = uncertain
        await bot.process_ai(bot.ai_state.claim())
        assert len(calls) == (2 if reason == "message to edit not found" else 1)
        request = bot.ai_state.request(1, note["id"])
        assert request["delivery"] == ("unknown" if reason == "network" else "sent")
        await bot.deliver_ai(request)
        assert len(calls) == (2 if reason == "message to edit not found" else 1)
        assert len(image_cli) == 1
    asyncio.run(run())


def test_image_long_answer_is_not_lost_to_caption_limit(bot, image_cli):
    async def run():
        image_assistant(bot)
        await receive(bot, "Нарисуй и объясни", reply=777)
        answer = "Объяснение. " * 450
        bot.assistant.responses = [step("create_image", content="Круг"), step(message=answer)]
        start = len(bot.telegram.sent)
        await bot.process_ai(bot.ai_state.claim())
        sent = bot.telegram.sent[start:]
        assert sent[0][0] == "editMessageMedia"
        assert all(m == "sendMessage" for m, _ in sent[1:])
        assert answer in "".join(p["text"] for _, p in sent[1:])
    asyncio.run(run())


@pytest.mark.parametrize("edit", [False, True])
def test_telegram_upload_photo_uses_multipart_and_remembers_exact_message(tmp_path, edit):
    async def run():
        path = tmp_path / "image.png"
        path.write_bytes(b"image-payload")
        calls, receipts = [], []
        def handler(request):
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content)
            parts = {p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
                     for p in message.iter_parts()}
            calls.append((request.url.path.rsplit("/", 1)[-1], parts))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 22,
                "photo": [{"file_id": "image-id"}]}})
        telegram = Telegram("123:test", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        telegram.on_message = lambda *args: receipts.append(args)
        await telegram.photo(1, path, thread=50, caption="🤖 Готово", keyboard={"inline_keyboard": []},
                             reply_to=11, edit_id=22 if edit else None)
        method, parts = calls[0]
        assert method == ("editMessageMedia" if edit else "sendPhoto")
        assert parts["photo"] == b"image-payload" and receipts == [(1, 22, 50)]
        if edit:
            assert parts["message_id"] == b"22" and "reply_parameters" not in parts
            assert json.loads(parts["media"])["media"] == "attach://photo"
            assert json.loads(parts["media"])["caption"] == "🤖 Готово"
        else:
            assert json.loads(parts["reply_parameters"])["message_id"] == 11
            assert parts["message_thread_id"] == b"50" and parts["caption"].decode() == "🤖 Готово"
        await telegram.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "emoji"])
def test_photo_upload_retries_only_explicit_emoji_rejection(tmp_path, failure):
    async def run():
        path = tmp_path / "image.png"
        path.write_bytes(b"image-payload")
        calls = []
        def handler(request):
            calls.append(request.content)
            if failure == "timeout":
                raise httpx.ReadTimeout("lost reply")
            if len(calls) == 1:
                return httpx.Response(400, json={"ok": False, "error_code": 400,
                    "description": "CUSTOM_EMOJI_ID_INVALID"})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 22}})
        telegram = Telegram("123:test", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        if failure == "timeout":
            with pytest.raises(ProviderError, match="telegram_transport_unknown"):
                await telegram.photo(1, path, caption="🤖 Готово", edit_id=22)
            assert len(calls) == 1
        else:
            await telegram.photo(1, path, caption="🤖 Готово", edit_id=22)
            assert len(calls) == 2 and b"caption_entities" not in calls[-1]
            assert all(b"image-payload" in content for content in calls)
        await telegram.close()
    asyncio.run(run())
