import asyncio
import sqlite3

import pytest
from support.commands import bot as bot
from support.commands import receive
from support.menus import labels, reply_keyboard

from voice_memory_bot.config import Limits
from voice_memory_bot.store import Store


def test_upgrade_seeds_four_pins_per_owner_and_restart_preserves_unpinning(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, active_folder TEXT, action TEXT);
            CREATE TABLE folders (
                id TEXT PRIMARY KEY, owner INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL, normalized TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]',
                thread_id INTEGER, is_inbox INTEGER NOT NULL DEFAULT 0,
                UNIQUE(owner, normalized), UNIQUE(owner, thread_id)
            );
        """)
        for owner in (1, 2):
            db.execute("INSERT INTO users(id,active_folder) VALUES (?,?)", (owner, f"{owner}-5"))
            for number, name in enumerate(("Входящие", "А", "Б", "В", "Г", "Д")):
                db.execute("INSERT INTO folders(id,owner,name,normalized,is_inbox) VALUES (?,?,?,?,?)",
                           (f"{owner}-{number}", owner, name, name.casefold(), int(number == 0)))
        before = db.execute("SELECT id,owner,name,thread_id FROM folders ORDER BY id").fetchall()
    store = Store(path, Limits())
    try:
        for owner in (1, 2):
            assert [f["id"] for f in store.pinned_folders(owner)] == [f"{owner}-{n}" for n in range(4)]
            assert store.current_folder(owner) == f"{owner}-5"
        assert [tuple(r) for r in store.db.execute(
            "SELECT id,owner,name,thread_id FROM folders ORDER BY id")] == before
        for folder in store.pinned_folders(1):
            store.set_folder_pin(1, folder["id"], False)
    finally:
        store.close()
    reopened = Store(path, Limits())
    try:
        assert reopened.pinned_folders(1) == []
        assert len(reopened.pinned_folders(2)) == 4
        assert reopened.current_folder(1) == "1-5"
        assert reopened.one("PRAGMA quick_check")["quick_check"] == "ok"
        assert reopened.all("PRAGMA foreign_key_check") == []
    finally:
        reopened.close()


def test_pin_limit_owner_isolation_and_freed_slot(bot):
    store = bot.store
    folders = [store.create_folder(1, str(n)) for n in range(4)]
    assert [f["pin_slot"] for f in store.pinned_folders(1)] == [1, 2, 3, 4]
    assert folders[-1]["pin_slot"] is None
    before = store.pinned_folders(1)
    with pytest.raises(ValueError, match="уже 4"):
        store.set_folder_pin(1, folders[-1]["id"], True)
    assert store.pinned_folders(1) == before
    with pytest.raises(ValueError, match="не найдена"):
        store.set_folder_pin(2, folders[0]["id"], False)
    store.set_folder_pin(1, folders[0]["id"], False)
    store.set_folder_pin(1, folders[-1]["id"], True)
    store.set_folder_pin(1, folders[-1]["id"], True)
    assert store.folder(1, folders[-1]["id"])["pin_slot"] == 2
    store.delete_folder(1, folders[-1]["id"])
    replacement = store.create_folder(1, "Новая")
    assert replacement["pin_slot"] == 2
    assert len(store.pinned_folders(1)) == 4


def test_folder_keys_select_before_input_or_ai_and_plain_names_are_notes(bot):
    async def run():
        work = bot.store.create_folder(1, "Работа")
        chat = bot.store.create_folder(1, "Диалог")
        bot.ai_state.mode(1, chat["id"], "chat")
        await receive(bot, "/start")
        assert labels(bot) == ["✅ Входящие", "📁 Работа", "📁 Диалог", "＋", "📁 Папка", "🎛️ Настройки"]
        assert reply_keyboard(bot)["is_persistent"]
        await receive(bot, "📁 Диалог", 2)
        assert "✅ Диалог" in labels(bot)
        assert bot.telegram.sent[-1][0] == "deleteMessage"
        await receive(bot, "/new", 3)
        await receive(bot, "📁 Работа", 4)
        assert bot.store.action(1) is None
        assert bot.store.current_folder(1) == work["id"]
        # A delayed button with the previous check mark is still a navigation action.
        await receive(bot, "✅ Входящие", 5)
        assert bot.store.current_folder(1) == bot.store.ensure_user(1)
        assert bot.store.notes(1) == []
        assert bot.store.all("SELECT * FROM ai_requests") == []
        await receive(bot, "Работа", 6)
        assert bot.store.notes(1)[0]["transcript"] == "Работа"
    asyncio.run(run())


def test_pinning_rename_delete_and_stale_keys_refresh_without_archiving(bot):
    async def run():
        work = bot.store.create_folder(1, "Работа")
        await receive(bot, "/start")
        await bot.callback(1, None, 500, f"unpin:{work['id']}")
        assert bot.store.folder(1, work['id'])['pin_slot'] is None
        assert bot.bottom_state(1)["screen"] == f"foldermenu:{work['id']}"
        await bot.callback(1, None, 500, f"foldermenu:{work['id']}")
        assert "📌 Закрепить внизу" not in labels(bot)
        await bot.callback(1, None, 500, f"pin:{work['id']}")
        assert bot.store.folder(1, work["id"])["pin_slot"] is not None
        await bot.callback(1, None, 500, f"rename:{work['id']}")
        await receive(bot, "Проекты", 2)
        assert "Проекты" in reply_keyboard(bot)["input_field_placeholder"]
        assert "📦 Экспорт" in labels(bot)
        await receive(bot, "📁 Работа", 3)
        assert "устарела" in next(p["text"] for m, p in reversed(bot.telegram.sent) if m == "sendMessage")
        assert bot.store.current_folder(1) == work["id"]
        await bot.callback(1, None, 500, f"dropfolder:{work['id']}")
        token = bot.folder_delete_confirmations[1][0]
        await bot.callback(1, None, 500, f"confirmdrop:{token}")
        await receive(bot, "✅ Проекты", 4)
        assert labels(bot) == ["✅ Входящие", "＋", "📁 Папка", "🎛️ Настройки"]
        assert bot.store.notes(1) == []
    asyncio.run(run())


def test_menu_keeps_all_actions_and_long_folder_names_are_distinct(bot):
    async def run():
        prefix = "Длинное название папки " * 3
        first = bot.store.create_folder(1, prefix + "А")
        second = bot.store.create_folder(1, prefix + "Б")
        bot.store.create_folder(1, "Четвёртая")
        hidden = bot.store.create_folder(1, "Пятая")
        await receive(bot, "/start")
        assert len(reply_keyboard(bot)["keyboard"]) == 3
        assert "📁 " + first["name"] in labels(bot)
        assert "📁 " + second["name"] in labels(bot)
        assert "📁 " + hidden["name"] not in labels(bot)
        await receive(bot, "📁 " + second["name"], 2)
        assert bot.store.current_folder(1) == second["id"]
        await receive(bot, "🎛️ Настройки", 3)
        callbacks = set(bot.bottom_state(1)["actions"].values())
        assert {f"menumessagebuttons:{0 if bot.store.message_buttons_enabled(1) else 1}", "help"} == callbacks
        await bot.callback(1, None, 500, "notes")
        assert bot.store.current_folder(1) == second["id"]
        await receive(bot, "📂 Все папки", 4)
        first_page = str(reply_keyboard(bot))
        await receive(bot, "Папки ➡️", 5)
        assert hidden["name"] in first_page + str(reply_keyboard(bot))
        assert bot.store.notes(1) == []
    asyncio.run(run())


def test_pages_reach_every_folder_and_arrows_never_become_notes(bot):
    async def run():
        for n in range(10):
            bot.store.create_folder(1, f"Проект {n}")
        await receive(bot, "/start")
        initial = bot.store.current_folder(1)
        seen = set()
        for page in range(3):
            keys = reply_keyboard(bot)["keyboard"]
            folder_labels = [b["text"] for row in keys for b in row if b["text"] != "📁 Папка" and b["text"].startswith(("📁 ", "✅ "))]
            assert len(folder_labels) <= 4
            seen.update(label.removeprefix("✅ ").removeprefix("📁 ") for label in folder_labels)
            if page < 2:
                assert keys[1][-1]["text"] == "➡️"
                await receive(bot, "➡️", 10 + page)
        assert seen == {f["name"] for f in bot.store.folders(1)}
        assert "➡️" not in labels(bot)
        assert bot.store.current_folder(1) == initial
        target = next(f for f in bot.store.folders(1) if "📁 " + f["name"] in labels(bot))
        await receive(bot, "📁 " + target["name"], 20)
        assert bot.store.current_folder(1) == target["id"]
        await receive(bot, "⬅️", 21)
        assert bot.folder_pages[1] == 1
        # Deleting folders clamps a stale final page; another owner is independent.
        bot.folder_pages[1] = 99
        for f in list(bot.store.folders(1)):
            if not f["is_inbox"]:
                bot.store.delete_folder(1, f["id"])
        await receive(bot, "➡️", 22)
        assert bot.folder_pages[1] == 0
        assert bot.store.notes(1) == []
        assert bot.store.all("SELECT * FROM ai_requests") == []
    asyncio.run(run())


def test_folder_page_turns_replace_short_carrier_and_preserve_keyboard(bot):
    async def run():
        bot.telegram.message_sequence = 1000
        for name in ("1", "2", "3", "4", "5", "6"):
            bot.store.create_folder(1, name)
        await receive(bot, "Запись", 100)
        await bot.show_home(1)
        original = bot.store.one("SELECT message_id FROM bottom_menus WHERE owner=1")["message_id"]
        folder = bot.store.current_folder(1)
        notes = bot.store.notes(1)
        sent = []
        original_text = bot.telegram.text

        async def record(*args, **kwargs):
            result = await original_text(*args, **kwargs)
            sent.append(result["message_id"])
            return result

        bot.telegram.text = record
        for command, page in [("➡️", 1), ("⬅️", 0), ("➡️", 1)]:
            bot.telegram.sent.clear()
            sent.clear()
            await receive(bot, command, 400)
            removed = {p["message_id"] for m, p in bot.telegram.sent if m == "deleteMessage"}
            carrier = bot.store.one("SELECT message_id FROM bottom_menus WHERE owner=1")["message_id"]
            assert [mid for mid in sent if mid not in removed] == [carrier]
            assert original in removed and 100 not in removed and 400 in removed
            message = next(p for m, p in reversed(bot.telegram.sent) if m == "sendMessage")
            assert message["text"] == f"Папки · {page + 1}/2"
            original = carrier
            assert bot.folder_pages[1] == page
            assert bot.store.current_folder(1) == folder and bot.store.notes(1) == notes
        target = next(f for f in bot.store.folders(1) if "📁 " + f["name"] in labels(bot))
        await receive(bot, "📁 " + target["name"], 500)
        assert bot.store.current_folder(1) == target["id"]
        assert bot.store.notes(1) == notes
    asyncio.run(run())
