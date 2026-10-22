"""Explicit opt-in live check: three synthetic notes, no Telegram messages or personal data."""
import argparse
import asyncio
import json
import time

from voice_memory_bot.config import LLMConfig
from voice_memory_bot.providers import CodexRouter, ProviderError
from voice_memory_bot.routing import decide


async def main(model):
    router = CodexRouter(LLMConfig(model=model))
    folders = [{"id": "inbox", "name": "Входящие", "aliases": "[]"},
               {"id": "game", "name": "Игра", "aliases": '["метро"]'},
               {"id": "work", "name": "Работа", "aliases": "[]"}]
    cases = [("Сохрани в проект Игра. Надо добавить большие кнопки в меню.", "game"),
             ("Надо обсудить новую игру с коллегами на работе.", "work"),
             ("Есть идея интерфейса. Это запиши в метро.", "game")]
    for i, (text, expected) in enumerate(cases, 1):
        started = time.monotonic()
        try:
            result = await router.classify(text, folders)
            decision = decide(text, folders, "work", "inbox", result)
            passed = decision.folder_id == expected
            print(json.dumps({"case": i, "model": model, "seconds": round(time.monotonic() - started, 2),
                              "folder": decision.folder_id, "status": decision.status, "passed": passed}), flush=True)
            if not passed:
                return 1
        except ProviderError as error:
            print(json.dumps({"case": i, "error": str(error)}), flush=True)
            return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.6-luna")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.model)))
