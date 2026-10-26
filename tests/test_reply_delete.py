import asyncio

import pytest
from support.base import make_bot, receive_voice
from support.commands import bot as bot
from support.commands import receive
from support.menus import labels


@pytest.mark.parametrize('target', [100, 200, None, 300, 999])
@pytest.mark.parametrize('command', ['/delete', '/delete@your_audio_notes_bot'])
def test_reply_delete_only_owned_record_after_confirmation(bot, target, command):
    async def run():
        bot.telegram.message_sequence = 1000
        await receive(bot, 'Сохранённая заметка', 100)
        note = bot.store.notes(1)[0]
        bot.messages.record(note, 200, None, 'transcript')
        directory = bot.archive.directory(note)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "note.md").write_text("Сохранённая заметка")
        bot.store.add_note(2, bot.store.ensure_user(2), {'message_id': 300}, 'text', transcript='Чужая')
        bot.store.set_action(1, 'create')
        message = {'message_id': 400, 'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'}, 'text': command}
        if target:
            message['reply_to_message'] = {'message_id': target}
        bot.telegram.sent.clear()
        await bot.handle({'message': message})
        assert len(bot.store.notes(1)) == 1
        assert len(bot.store.notes(2)) == 1
        assert directory.exists()
        sent = [p for m, p in bot.telegram.sent if m == 'sendMessage']
        if target in {100, 200, None}:
            assert sent[-1]['text'] == 'Удалить запись?'
            assert sent[-1]['reply_to'] == (target or 200)
            inline = sent[-1]['keyboard']['inline_keyboard']
            assert [[b['text'] for b in row] for row in inline] == [['Удалить', 'Отмена']]
            assert len(sent) == 1
            assert 'keyboard' not in sent[0]['keyboard']
            state = bot.bottom_state(1)
            assert not any(m == 'deleteMessage' and p['message_id'] == state['inline_prompt_id']
                           for m, p in bot.telegram.sent)
            await bot.callback(1, None, state['inline_prompt_id'], inline[0][0]['callback_data'])
            assert not bot.store.notes(1)
            assert not directory.exists()
            notice_index = next(i for i, (m, p) in enumerate(bot.telegram.sent)
                                if m == 'sendMessage' and p.get('text') == 'Сообщение удалено.')
            assert all(i > notice_index for i, (m, p) in enumerate(bot.telegram.sent)
                       if m == 'deleteMessage' and p['message_id'] in {100, 200})
            assert bot.store.one('SELECT transcript FROM notes WHERE id=?', (note['id'],))['transcript'] is None
        deleted = {p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage'}
        assert 300 not in deleted
        if target is not None:
            assert 400 not in deleted
        if target in {100, 200, None}:
            assert {100, 200} <= deleted
            assert (400 in deleted) == (target is None)
        else:
            assert not ({100, 200, None} & deleted)
    asyncio.run(run())


def test_processing_record_cannot_be_deleted(bot):
    async def run():
        await receive(bot, 'Ещё обрабатывается', 100)
        note = bot.store.notes(1)[0]
        bot.store.update_note(1, note['id'], state='processing')
        await bot.handle({'message': {'message_id': 400, 'from': {'id': 1},
            'chat': {'id': 1, 'type': 'private'}, 'text': '/delete', 'reply_to_message': {'message_id': 100}}})
        assert bot.store.note(1, note['id'])
        assert any('Дождись завершения' in p.get('text', '') for m, p in bot.telegram.sent)
    asyncio.run(run())


def test_minus_removed_and_stale_button_does_not_create_note(bot):
    async def run():
        for command in ['/start', '/settings', '/folders', '➖']:
            await receive(bot, command)
            assert '➖' not in labels(bot)
        assert bot.store.notes(1) == []
    asyncio.run(run())


@pytest.mark.parametrize('inline', [False, True])
@pytest.mark.parametrize('cancel', [False, True])
def test_delete_confirmation_shared_target_and_single_use(bot, inline, cancel):
    async def run():
        bot.telegram.message_sequence = 1000
        await receive(bot, 'Первая', 100)
        first = bot.store.notes(1)[0]
        await receive(bot, '/delete', 200)
        state = bot.bottom_state(1)
        await receive(bot, 'Вторая', 300)
        label = 'Отмена' if cancel else 'Удалить'
        if inline:
            await bot.callback(1, None, state['inline_prompt_id'], state['actions'][label])
        else:
            await receive(bot, label, 400)
        assert len(bot.store.notes(1)) == (2 if cancel else 1)
        assert bot.store.one('SELECT deleted FROM notes WHERE id=?', (first['id'],))['deleted'] == (not cancel)
        assert bot.bottom_state(1)['screen'] == 'root'
        await bot.callback(1, None, state['inline_prompt_id'], state['actions']['Удалить'])
        assert len(bot.store.notes(1)) == (2 if cancel else 1)
    asyncio.run(run())


def test_delete_old_confirmation_cannot_delete_after_new_prompt_or_restart(bot, tmp_path):
    from support.base import make_bot
    async def run():
        bot.telegram.message_sequence = 1000
        await receive(bot, 'Запись', 100)
        await receive(bot, '/delete', 200)
        old = bot.bottom_state(1)
        await receive(bot, '/delete', 300)
        await bot.callback(1, None, old['inline_prompt_id'], old['actions']['Удалить'])
        assert len(bot.store.notes(1)) == 1
        state = bot.bottom_state(1)
        restarted = make_bot(tmp_path, bot.store)
        await restarted.callback(1, None, state['inline_prompt_id'], state['actions']['Удалить'])
        assert len(bot.store.notes(1)) == 1
    asyncio.run(run())


@pytest.mark.parametrize("previous_menu", ["present", "deleted", "absent"])
@pytest.mark.parametrize("restart", [False, True])
def test_cancel_latest_voice_keeps_folder_keyboard_and_placeholder(bot, tmp_path, previous_menu, restart):
    async def run():
        bot.telegram.message_sequence = 1000
        folder = bot.store.current_folder(1)
        bot.store.rename_folder(1, folder, "Голосовые")
        if previous_menu != "absent":
            await bot.show_home(1)
            if previous_menu == "deleted":
                old = bot.store.one("SELECT message_id FROM bottom_menus WHERE owner=1")
                await bot.remove_menu_message(1, old["message_id"])
        await receive_voice(bot, 100, None)
        note = bot.store.notes(1)[0]
        bot.store.update_note(1, note["id"], state="ready", delivery="sent", transcript_delivery="sent")
        original = bot.store.note(1, note["id"])
        await receive(bot, "/delete", 200)
        state = bot.bottom_state(1)
        assert state["screen"] == f"notedelete:{note['id']}"
        current = make_bot(tmp_path, bot.store) if restart else bot
        current.telegram.message_sequence = bot.telegram.message_sequence
        sent = []
        send = current.telegram.text

        async def capture(*args, **kwargs):
            result = await send(*args, **kwargs)
            sent.append((result["message_id"], args[3] if len(args) > 3 else kwargs.get("keyboard")))
            return result

        current.telegram.text = capture
        await current.callback(1, None, state["inline_prompt_id"], state["actions"]["Отмена"])
        carrier, markup = next((mid, keys) for mid, keys in reversed(sent) if keys and "keyboard" in keys)
        removed = {p["message_id"] for m, p in current.telegram.sent if m == "deleteMessage"}
        assert carrier not in removed
        assert current.store.one("SELECT message_id FROM bottom_menus WHERE owner=1")["message_id"] == carrier
        assert markup["is_persistent"] and not markup["one_time_keyboard"]
        assert markup["input_field_placeholder"] == "📁 Голосовые"
        assert "✅ Голосовые" in [b["text"] for row in markup["keyboard"] for b in row]
        assert state["inline_prompt_id"] in removed
        assert 100 not in removed
        assert current.store.note(1, note["id"]) == original
        assert current.store.current_folder(1) == folder
    asyncio.run(run())


@pytest.mark.parametrize("reply", [False, True])
@pytest.mark.parametrize("remaining_copy", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_delete_recovers_missing_telegram_target(bot, reply, remaining_copy, cancel):
    from voice_memory_bot.providers import Telegram, TelegramError

    async def run():
        await receive(bot, "Именно эта запись", 100)
        note = bot.store.notes(1)[0]
        bot.messages.record(note, 200, None, "transcript")
        foreign, _ = bot.store.add_note(2, bot.store.ensure_user(2), {"message_id": 900},
                                     "text", transcript="Чужая")
        bot.messages.record(foreign, 900, None, "source")
        attempts, delivered = [], []
        telegram = Telegram("test:token")
        available = {100} if remaining_copy else set()
        sequence = 1000

        async def request(method, payload):
            nonlocal sequence
            if method == "sendMessage":
                target = (payload.get("reply_parameters") or {}).get("message_id")
                attempts.append(target)
                if target and target not in available:
                    raise TelegramError(400, method, "Bad Request: message to be replied not found")
                sequence += 1
                available.add(sequence)
                delivered.append((sequence, payload))
                return {"message_id": sequence}
            if method == "deleteMessage":
                available.discard(payload["message_id"])
            return True

        telegram._request = request
        bot.telegram = telegram
        try:
            message = {"message_id": 400, "from": {"id": 1},
                       "chat": {"id": 1, "type": "private"}, "text": "/delete"}
            if reply:
                message["reply_to_message"] = {"message_id": 200}
            await bot.handle({"message": message})
            state = bot.bottom_state(1)
            assert state["screen"] == f"notedelete:{note['id']}"
            assert attempts[:2] == [200, 100]
            assert 900 not in attempts
            assert [p["text"] for _, p in delivered] == (
                ["Удалить запись?"] if remaining_copy
                else ["Запись из архива:\nИменно эта запись", "Удалить запись?"])
            prompt_id, prompt = delivered[-1]
            target = prompt["reply_parameters"]["message_id"]
            assert target == (100 if remaining_copy else delivered[0][0])
            assert state["reply_to"] == target and state["inline_prompt_id"] == prompt_id
            assert prompt_id in available
            assert [[b["text"] for b in row] for row in prompt["reply_markup"]["inline_keyboard"]] == [
                ["Удалить", "Отмена"]]
            assert bot.store.note(1, note["id"])
            assert bot.store.one("SELECT deleted FROM note_messages WHERE owner=1 AND message_id=200")["deleted"]
            label = "Отмена" if cancel else "Удалить"
            await bot.callback(1, None, prompt_id, state["actions"][label])
            assert bool(bot.store.notes(1)) == cancel
            assert len(bot.store.notes(2)) == 1
            await bot.callback(1, None, prompt_id, state["actions"][label])
            assert bool(bot.store.notes(1)) == cancel
        finally:
            await telegram.close()
    asyncio.run(run())


@pytest.mark.parametrize("error", ["transport", "other_400"])
def test_delete_does_not_retry_unknown_or_unrelated_send_failure(bot, error):
    from voice_memory_bot.providers import ProviderError, TelegramError

    async def run():
        await receive(bot, "Запись", 100)
        calls = []

        async def fail(*args, **kwargs):
            calls.append(kwargs.get("reply_to"))
            if error == "transport":
                raise ProviderError("telegram_transport_unknown")
            raise TelegramError(400, "sendMessage", "Bad Request: chat not found")

        bot.telegram.text = fail
        bot.telegram.sent.clear()
        with pytest.raises(ProviderError):
            await receive(bot, "/delete", 400)
        assert calls == [100]
        assert len(bot.store.notes(1)) == 1
        assert not any(m == "deleteMessage" and p["message_id"] == 400 for m, p in bot.telegram.sent)
    asyncio.run(run())
