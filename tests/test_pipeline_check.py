import asyncio
import importlib.util
import json
import wave
from pathlib import Path

from voice_memory_bot.config import Config


def test_smoke_check_uses_selected_folder_and_preserves_original(monkeypatch, capsys, tmp_path):
    path = Path(__file__).parents[1] / "scripts/check_pipeline.py"
    spec = importlib.util.spec_from_file_location("pipeline_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def synthesize(args, **kwargs):
        with wave.open(args[-1], "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 16000)

    class Speech:
        async def transcribe(self, path):
            return "Save this in another folder. Keep the complete original."

        async def close(self):
            pass

    async def no_classifier(*args):
        raise AssertionError("Selected folders must not depend on a classifier")

    monkeypatch.setattr(module.subprocess, "run", synthesize)
    monkeypatch.setattr(module, "Groq", lambda *args: Speech())
    monkeypatch.setattr(module.CodexRouter, "classify", no_classifier)
    code = asyncio.run(module.check(Config("1:token", frozenset({1}), "secret", tmp_path)))
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["selected_folder_preserved"]
    assert result["original_export_unchanged"] and result["telegram_messages_sent"] == 0
