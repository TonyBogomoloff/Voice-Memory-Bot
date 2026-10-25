import json
from pathlib import Path

import pytest
from PIL import Image

from support.ai import step
from voice_memory_bot.image_generation import ImageRouter, generate_image


@pytest.fixture
def image_cli(monkeypatch):
    calls = []

    async def run(self, args, **kwargs):
        if args == ["login", "status"]:
            return 0, b"Logged in using ChatGPT"
        calls.append((args, kwargs, self.home))
        work = Path(args[args.index("-C") + 1])
        artifact = self.home / "generated_images" / "run" / "result.png"
        artifact.parent.mkdir(parents=True)
        Image.new("RGB", (16, 16), "orange").save(artifact)
        (work / "result.json").write_text(json.dumps({"image_path": str(artifact), "error": ""}))
        return 0, b""

    monkeypatch.setattr(ImageRouter, "_run", run)
    return calls


def image_assistant(bot):
    async def respond(payload, model, **kwargs):
        bot.assistant.calls.append((payload, model))
        return bot.assistant.responses.pop(0) if bot.assistant.responses else step(message="Картинка готова")

    async def generate(prompt, model, job, maximum, source=None):
        return await generate_image(bot.config.llm, prompt, model, job, maximum, source)

    bot.assistant.respond = respond
    bot.assistant.generate_image = generate


def photo(bot, owner=1):
    note, _ = bot.store.add_note(owner, bot.store.ensure_user(owner), {"message_id": 100},
                                "photo", extension="png", transcript="")
    bot.store.update_note(owner, note["id"], state="ready", origin="web", mime_type="image/png")
    note = bot.store.note(owner, note["id"])
    path = bot.archive.audio(note)
    path.parent.mkdir(parents=True)
    Image.new("RGB", (16, 16), "blue").save(path)
    bot.store.update_note(owner, note["id"], audio_bytes=path.stat().st_size)
    return bot.store.note(owner, note["id"])
