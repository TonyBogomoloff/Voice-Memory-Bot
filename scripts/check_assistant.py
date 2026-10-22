"""Opt-in live model check. Only synthetic data in a temporary archive; never sends Telegram messages."""
import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

from voice_memory_bot.ai_store import AIStore
from voice_memory_bot.assistant import AssistantEngine, CodexAssistant
from voice_memory_bot.config import Limits, LLMConfig
from voice_memory_bot.providers import ProviderError
from voice_memory_bot.store import Store


async def main(model):
    provider = CodexAssistant(LLMConfig(model=model, timeout_seconds=120))
    started = time.monotonic()
    cases = [("Луна, помоги составить план", True), ("Что думаешь об этой идее, Луна?", True),
             ("Сегодня на небе красивая луна", False), ("Он сказал: «Луна, помоги составить план»", False)]
    results = await asyncio.gather(*(provider.addressed(text, ["Луна"], model) for text, _ in cases))
    for index, (actual, (_, expected)) in enumerate(zip(results, cases), 1):
        print(json.dumps({"address_case": index, "passed": actual == expected}), flush=True)
    if any(actual != expected for actual, (_, expected) in zip(results, cases)):
        return 1
    with tempfile.TemporaryDirectory(prefix="voice-memory-ai-check-") as directory:
        store = Store(Path(directory) / "archive.sqlite3", Limits())
        try:
            inbox = store.ensure_user(1)
            state = AIStore(store)
            engine = AssistantEngine(state, provider, model, (model,))
            conversation = state.new_conversation(1, inbox)
            prompts = [
                "Создай во Входящих заметку с заголовком 'План теста' и текстом 'Купить зелёный блокнот'.",
                "В созданной заметке замени зелёный на синий, остальное сохрани.",
                "Найди в моих заметках слово 'синий' и скажи, что нужно купить.",
            ]
            for index, prompt in enumerate(prompts, 1):
                note, _ = store.add_note(1, inbox, {"message_id": index}, "text", transcript=prompt)
                store.update_note(1, note["id"], state="ready")
                state.enqueue(1, note["id"], "button", conversation=conversation)
                await engine.run(state.claim())
                request = state.request(1, note["id"])
                print(json.dumps({"conversation_case": index, "state": request["state"],
                                  "actions": [s["command"]["action"] for s in state.steps(1, note["id"])],
                                  "response": request["response"]}, ensure_ascii=False), flush=True)
            notes = store.notes(1)
            passed = (len(notes) == 1 and "синий" in (notes[0]["content"] or "")
                      and "зелёный" in notes[0]["transcript"] and notes[0]["revision"] == 1)
            print(json.dumps({"archive_passed": passed, "seconds": round(time.monotonic() - started, 1)}), flush=True)
            return 0 if passed else 1
        finally:
            store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True, help="Use the authenticated model")
    parser.add_argument("--model", default="gpt-5.6-luna")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(main(args.model)))
    except (ProviderError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from None
