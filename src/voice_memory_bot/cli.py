from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import shutil
from pathlib import Path

from .bot import Bot
from .config import Config
from .providers import CodexRouter, Groq, ProviderError, Telegram
from .store import Store


async def check(config, live):
    result = {"config": "valid", "allowed_users": len(config.allowed_user_ids),
              "asr": config.groq_model, "llm": config.llm.model, "effort": config.llm.effort,
              "ffprobe": bool(shutil.which("ffprobe")), "codex": bool(shutil.which(config.llm.binary))}
    if live:
        router = CodexRouter(config.llm)
        await router.check_auth()
        result["llm_auth"] = "ChatGPT"
        telegram = Telegram(config.bot_token, api_base=config.telegram_api_base, file_root=config.telegram_file_root)
        try:
            me = await telegram.call("getMe")
            result["bot"] = me.get("username")
            result["topics"] = bool(me.get("has_topics_enabled"))
            result["webhook_present"] = bool((await telegram.call("getWebhookInfo")).get("url"))
        finally:
            await telegram.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ffprobe"] and result["codex"] else 1


async def run(config):
    if not shutil.which("ffprobe"):
        raise ValueError("Установи ffmpeg: для проверки длительности нужен ffprobe")
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.data_dir.chmod(0o700)
    with (config.data_dir / "runtime.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Для этого data_dir бот уже запущен") from None
        store = Store(config.data_dir / "archive.sqlite3", config.limits)
        telegram = Telegram(config.bot_token, api_base=config.telegram_api_base, file_root=config.telegram_file_root)
        groq = Groq(config.groq_voice, config.groq_model)
        try:
            await Bot(config, store, telegram, groq, CodexRouter(config.llm)).start()
        finally:
            store.close()
            await telegram.close()
            await groq.close()


def main():
    parser = argparse.ArgumentParser(description="Voice Memory Bot")
    parser.add_argument("command", choices=["run", "check"])
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--secrets", type=Path, help="Общий конфиг: bot_token_your_audio_notes и groq_voice")
    parser.add_argument("--live", action="store_true", help="check: проверить getMe и вход Codex без отправки сообщений")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx INFO logs request URLs, including the Telegram bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        config = Config.load(args.config, args.secrets)
        if args.command == "check":
            raise SystemExit(asyncio.run(check(config, args.live)))
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass
    except (ProviderError, ValueError) as exc:
        # Config validation and ProviderError use controlled, non-sensitive descriptions.
        if isinstance(exc, json.JSONDecodeError):
            print("Ошибка: конфигурация содержит некорректный JSON")
        else:
            print(f"Ошибка: {exc}")
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"Ошибка запуска: {type(exc).__name__}; секреты и ответы провайдеров скрыты")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
