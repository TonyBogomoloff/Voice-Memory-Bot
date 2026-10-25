"""Opt-in live Groq → selected folder → archive check with synthetic speech.

No messages are sent to Telegram. Requires ffmpeg with its built-in flite filter.
"""
import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import replace
from pathlib import Path

from voice_memory_bot.bot import Bot
from voice_memory_bot.config import Config
from voice_memory_bot.providers import CodexRouter, Groq, ProviderError
from voice_memory_bot.store import Store


class LocalTelegram:
    def __init__(self, source):
        self.source = source
        self.sent = 0

    async def download(self, file_id, destination, max_bytes):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.source, destination)
        data = destination.read_bytes()
        return len(data), hashlib.sha256(data).hexdigest()

    async def text(self, *args, **kwargs):
        self.sent += 1
        return {"message_id": self.sent}

    async def call(self, method, **kwargs):
        if method.startswith("editMessage"):
            return {"message_id": kwargs["message_id"]}
        if method == "deleteMessage":
            return True
        raise AssertionError(f"unexpected_telegram_method:{method}")


async def check(config):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="voice-memory-live-") as directory:
        root = Path(directory)
        audio = root / "synthetic.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "flite=text='Save this in project Game. Make the menu buttons bigger.':voice=slt",
            "-ar", "16000", "-ac", "1", str(audio)], check=True, timeout=30,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        config = replace(config, data_dir=root / "app")
        store = Store(root / "smoke.sqlite3", config.limits)
        groq = Groq(config.groq_voice, config.groq_model)
        try:
            owner = next(iter(config.allowed_user_ids))
            store.ensure_user(owner)
            project = store.create_folder(owner, "Game")
            store.select(owner, project["id"])
            note, _ = store.add_note(owner, store.current_folder(owner), {"message_id": 1}, "voice", "synthetic-only",
                                     "wav", audio.stat().st_size, 10)
            bot = Bot(config, store, LocalTelegram(audio), groq, CodexRouter(config.llm))
            await bot.process(store.claim_note())
            saved = store.note(owner, note["id"])
            paths = bot.archive.export([saved], store.folders(owner), root / "export", True, 45000000)
            with zipfile.ZipFile(paths[0]) as archive:
                filename = next(n for n in archive.namelist() if n.endswith(".wav"))
                original_ok = archive.read(filename) == audio.read_bytes()
            result = {"asr": config.groq_model,
                      "transcription_nonempty": bool(saved["transcript"]),
                      "selected_folder_preserved": saved["folder_id"] == project["id"],
                      "original_export_unchanged": original_ok, "state": saved["state"],
                      "error": saved["error"], "telegram_messages_sent": 0,
                      "elapsed_seconds": round(time.monotonic() - started, 2)}
            print(json.dumps(result), flush=True)
            return 0 if all([result["transcription_nonempty"], result["selected_folder_preserved"],
                             original_ok, saved["state"] == "ready"]) else 1
        finally:
            await groq.close()
            store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--secrets", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        raise SystemExit(asyncio.run(check(Config.load(args.config, args.secrets))))
    except (ProviderError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, ProviderError) else type(exc).__name__}))
        raise SystemExit(1) from None
