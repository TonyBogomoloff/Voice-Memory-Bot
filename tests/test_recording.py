import asyncio

from test_mvp import make_bot
from test_mvp import store as store


def message(identity, prompt, **content):
    return {"message_id": identity, "from": {"id": 1}, "chat": {"id": 1, "type": "private"},
            "reply_to_message": {"message_id": prompt}, **content}


def test_record_prompts_route_interleaved_voice_after_restart(tmp_path, store):
    async def run():
        store.create_folder(1, "Test1", thread=100)
        store.create_folder(1, "Test2", thread=200)
        bot = make_bot(tmp_path, store)
        bot.telegram.message_sequence = 1000
        await bot.start_recording(1, 100)
        await bot.start_recording(1, 200)
        assert bot.telegram.sent[-1][1]["keyboard"]["force_reply"] is True
        bot = make_bot(tmp_path, store)
        bot.telegram.message_sequence = 2000
        for identity, prompt, thread in [(10, 1002, 200), (11, 1001, 100)]:
            await bot.handle({"message": message(identity, prompt,
                voice={"file_id": "voice", "file_size": 32044, "duration": 1})})
            await bot.process(store.claim_note())
            note = store.one("SELECT * FROM notes WHERE message_id=?", (identity,))
            assert note["source_thread"] == note["result_thread"] == thread
            assert note["delivery"] == "sent"
        assert not any(method == "sendVoice" for method, _ in bot.telegram.sent)
        assert bot.recording_reply(2, message(12, 1001)) is None
        assert bot.recording_reply(1, message(12, 1001, message_thread_id=200)) is None
    asyncio.run(run())


def test_old_topic_panel_names_existing_topic_once(tmp_path, store):
    async def run():
        store.mark_implicit_topic(1, 100)
        bot = make_bot(tmp_path, store)
        bot.topics_enabled = True
        bot.telegram.message_sequence = 1000
        await bot.install_record_panels()
        count = len(bot.telegram.sent)
        await bot.install_record_panels()
        assert len(bot.telegram.sent) == count
        await bot.start_recording(1, 100)
        prompt = bot.telegram.message_sequence
        await bot.handle({"message": message(10, prompt, text="Test1")})
        assert store.folder_for_thread(1, 100)["name"] == "Test1"
        assert not store.notes(1)
        assert store.one("SELECT purpose FROM record_prompts WHERE message_id=?", (prompt,))["purpose"] == "done"
        assert bot.telegram.sent[-1][1]["keyboard"]["force_reply"] is True
        assert not any(method == "createForumTopic" for method, _ in bot.telegram.sent)
    asyncio.run(run())


def test_uncertain_panel_is_not_duplicated_after_restart(tmp_path, store):
    async def run():
        store.create_folder(1, "Test1", thread=100)
        bot = make_bot(tmp_path, store)
        bot.topics_enabled = True
        bot.telegram.fail_delivery = True
        await bot.ensure_record_panel(1, 100)
        bot = make_bot(tmp_path, store)
        bot.topics_enabled = True
        await bot.ensure_record_panel(1, 100)
        assert not bot.telegram.sent
        assert store.one("SELECT state FROM record_panels WHERE thread_id=100")["state"] == "unknown"
    asyncio.run(run())


def test_existing_archive_can_be_linked_without_duplicate_or_lost_notes(tmp_path, store):
    async def run():
        folder = store.create_folder(1, "Test2", thread=100)
        note, _ = store.add_note(1, folder["id"], {"message_id": 10}, "text", transcript="keep")
        store.mark_implicit_topic(1, 200)
        bot = make_bot(tmp_path, store)
        bot.topics_enabled = True
        bot.telegram.message_sequence = 1000
        await bot.install_record_panels()
        await bot.start_recording(1, 200)
        prompt = bot.telegram.message_sequence
        await bot.handle({"message": message(20, prompt, text="Test2")})
        assert store.folder(1, folder["id"])["thread_id"] == 100
        callback = bot.telegram.sent[-1][1]["keyboard"]["inline_keyboard"][0][0]["callback_data"]
        await bot.callback(1, 200, 2000, callback)
        assert store.folder(1, folder["id"])["thread_id"] == 200
        assert store.note(1, note["id"])["folder_id"] == folder["id"]
        assert len([f for f in store.folders(1) if f["name"] == "Test2"]) == 1
        assert bot.recording_reply(1, message(21, prompt)) is None
        assert not any(method in {"createForumTopic", "deleteForumTopic", "sendVoice"} for method, _ in bot.telegram.sent)
    asyncio.run(run())


def test_import_does_not_silently_make_duplicate_folder(store):
    import pytest
    folder = store.create_folder(1, "Test2", thread=100)
    store.aliases(1, folder["id"], ["Second"])
    for name in ["Test2", "TEST2", "Second"]:
        with pytest.raises(ValueError):
            store.import_topic(1, name, 200)
    assert store.folder_for_thread(1, 200) is None
