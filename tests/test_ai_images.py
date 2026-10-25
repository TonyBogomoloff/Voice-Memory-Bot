import asyncio
import json
from dataclasses import replace

import pytest
from PIL import Image
from support.ai import bot as bot
from support.ai import ready_note, step
from support.images import image_assistant, photo
from support.images import image_cli as image_cli

from voice_memory_bot import ai_images
from voice_memory_bot.image_generation import ImageRouter, generate_image
from voice_memory_bot.providers import ProviderError


def test_create_and_edit_save_media_once_keep_source_and_export(bot, image_cli, tmp_path):
    async def run():
        image_assistant(bot)
        source = photo(bot)
        before = bot.archive.audio(source).read_bytes()
        note = ready_note(bot, "Перекрась в оранжевый", message_id=10)
        request = bot.ai_state.enqueue(1, note["id"], "button")
        bot.assistant.responses = [step("read_note", note_id=source["id"]),
            step("edit_image", note_id=source["id"], title="Оранжевый", content="Перекрась в оранжевый")]
        await bot.process_ai(bot.ai_state.claim())
        receipts = bot.ai_engine.receipts(1, note["id"])
        result = bot.store.note(1, receipts[0]["id"])
        assert result["kind"] == "photo" and result["state"] == "ready" and result["origin"] == "web"
        assert result["folder_id"] == source["folder_id"] and result["mime_type"] == "image/png"
        assert result["ai_generated_from"] == note["id"]
        assert Image.open(bot.archive.audio(result)).getpixel((0, 0)) == (255, 165, 0)
        assert bot.archive.audio(source).read_bytes() == before
        assert bot.ai_state.request(1, note["id"])["state"] == "ready"
        assert bot.telegram.sent[-1][0] == "editMessageMedia"
        assert bot.assistant.calls[1][0]["image_generation"]
        assert "--image" in image_cli[0][0]
        assert image_cli[0][0][image_cli[0][0].index("--sandbox") + 1] == "danger-full-access"
        assert "features.shell_tool=false" in image_cli[0][0] and image_cli[0][1]["timeout"] == 600
        assert not image_cli[0][2].exists()  # CLI scratch, login link and reference are removed.
        await bot.ai_engine.run(request)
        assert len(image_cli) == 1 and len(bot.store.all("SELECT id FROM notes WHERE owner=1 AND deleted=0")) == 4
        export = bot.archive.export([result], bot.store.folders(1), tmp_path / "zip", True,
                                    100000, text_format="markdown")
        assert export and result["sha256"]
        created = ready_note(bot, "Нарисуй", message_id=11)
        bot.ai_state.enqueue(1, created["id"], "button")
        bot.assistant.responses = [step("create_image", content="Круг")]
        await bot.process_ai(bot.ai_state.claim())
        assert len(image_cli) == 2 and "--image" not in image_cli[1][0]
    asyncio.run(run())


def test_edit_rejects_foreign_unread_and_readonly_sources_without_generator(bot, image_cli):
    async def run():
        image_assistant(bot)
        own, foreign = photo(bot), photo(bot, 2)
        note = ready_note(bot, message_id=10)
        request = bot.ai_state.enqueue(1, note["id"], "button")
        for index, source in enumerate((foreign, own, own)):
            if index == 2:
                bot.ai_state.set_settings(1, notes_access="read")
            bot.ai_state.save_command(1, note["id"], index, step("edit_image", note_id=source["id"], content="Синий"))
            assert (await ai_images.execute_image(bot.ai_engine, request, index))["error"]
        assert not image_cli
    asyncio.run(run())


@pytest.mark.parametrize("change", ["access", "quota"])
def test_recheck_after_generation_and_resume_durable_output(bot, image_cli, change):
    async def run():
        image_assistant(bot)
        note = ready_note(bot, message_id=10)
        request = bot.ai_state.enqueue(1, note["id"], "button")
        bot.ai_state.save_command(1, note["id"], 0, step("create_image", content="Круг"))
        original = bot.assistant.generate_image
        limits = bot.store.limits
        async def generate(*args, **kwargs):
            path = await original(*args, **kwargs)
            if change == "access":
                bot.ai_state.set_settings(1, notes_access="read")
            else:
                bot.store.limits = replace(limits, max_user_storage_bytes=1)
            return path
        bot.assistant.generate_image = generate
        with pytest.raises(ValueError):
            await ai_images.execute_image(bot.ai_engine, request, 0)
        assert len(bot.store.all("SELECT id FROM notes WHERE owner=1 AND deleted=0")) == 1 and len(image_cli) == 1
        assert bot.ai_state.steps(1, note["id"])[0]["result"] is None
        bot.ai_state.set_settings(1, notes_access="write")
        bot.store.limits = limits
        bot.assistant.generate_image = original
        result = await ai_images.execute_image(bot.ai_engine, request, 0)
        assert result["ok"] and len(image_cli) == 1
        assert await ai_images.execute_image(bot.ai_engine, request, 0) == result
        assert len(bot.store.all("SELECT id FROM notes WHERE owner=1 AND deleted=0")) == 2
    asyncio.run(run())


def test_storage_failure_rolls_back_note_and_receipt_then_reuses_image(bot, image_cli, monkeypatch):
    async def run():
        image_assistant(bot)
        note = ready_note(bot, message_id=10)
        request = bot.ai_state.enqueue(1, note["id"], "button")
        bot.ai_state.save_command(1, note["id"], 0, step("create_image", content="Круг"))
        write = ai_images.durable_bytes
        def fail(path, data):
            write(path, data)
            raise OSError("disk failure")
        monkeypatch.setattr(ai_images, "durable_bytes", fail)
        with pytest.raises(OSError):
            await ai_images.execute_image(bot.ai_engine, request, 0)
        assert len(bot.store.all("SELECT id FROM notes WHERE owner=1 AND deleted=0")) == 1
        assert bot.ai_state.steps(1, note["id"])[0]["result"] is None
        assert not list(bot.archive.root.rglob("upload.png"))
        monkeypatch.setattr(ai_images, "durable_bytes", write)
        assert (await ai_images.execute_image(bot.ai_engine, request, 0))["ok"]
        assert len(image_cli) == 1
    asyncio.run(run())


@pytest.mark.parametrize("artifact", ["outside", "symlink", "invalid"])
def test_generator_rejects_arbitrary_paths_and_nonimages(bot, monkeypatch, tmp_path, artifact):
    async def run(self, args, **kwargs):
        if args == ["login", "status"]:
            return 0, b"Logged in using ChatGPT"
        outside = tmp_path / "secret.png"
        Image.new("RGB", (2, 2)).save(outside)
        result = self.home / "generated_images" / "output.png"
        result.parent.mkdir(parents=True)
        if artifact == "outside":
            result = outside
        elif artifact == "symlink":
            result.symlink_to(outside)
        else:
            result.write_bytes(b"not an image")
        (self.home.parent / "result.json").write_text(json.dumps({"image_path": str(result), "error": ""}))
        return 0, b""
    monkeypatch.setattr(ImageRouter, "_run", run)
    with pytest.raises(ProviderError):
        asyncio.run(generate_image(bot.config.llm, "Картинка", "model", tmp_path / "job", 10000))
    assert not (tmp_path / "job/output.png").exists()


def test_image_progress_and_failed_generation_can_retry(bot, monkeypatch):
    async def run():
        image_assistant(bot)
        note = ready_note(bot, message_id=10)
        bot.ai_state.enqueue(1, note["id"], "button")
        bot.assistant.responses = [step("create_image", content="Круг")]
        async def failed(*args, **kwargs):
            assert bot.ai_state.request(1, note["id"])["state"] == "running"
            saved = bot.ai_state.steps(1, note["id"])[-1]
            assert saved["command"]["action"] == "create_image" and saved["result"] is None
            raise ProviderError("image_generation_failed")
        bot.assistant.generate_image = failed
        await bot.process_ai(bot.ai_state.claim())
        assert bot.ai_state.request(1, note["id"])["state"] == "failed"
        assert len(bot.store.all("SELECT id FROM notes WHERE owner=1 AND deleted=0")) == 1
        assert f"airetry:{note['id']}" in str(bot.telegram.sent[-1])
    asyncio.run(run())
