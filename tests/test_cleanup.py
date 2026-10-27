import asyncio
import json

import httpx
import pytest
from support.base import make_bot
from support.commands import receive
from support.menus import reply_keyboard

from voice_memory_bot.bot import HOME
from voice_memory_bot.providers import Telegram


def test_retired_account_cleanup_cannot_erase_data_even_from_saved_buttons(tmp_path, store):
    async def run():
        bot = make_bot(tmp_path, store)
        folder = store.current_folder(1)
        note, _ = store.add_note(1, folder, {'message_id': 1000}, 'text', transcript='Сохранить')
        bot.archive.persist_text(note, store.folder(1, folder))
        await bot.settings_menu(1, None)
        assert not any('Удалить всё' in b['text'] for row in reply_keyboard(bot)['keyboard'] for b in row)
        assert not any(b['callback_data'] == 'deleteall' for row in HOME['inline_keyboard'] for b in row)
        await receive(bot, '/delete_all', 2000)
        for action in ('deleteall', 'confirmdeleteall:old', 'retrydeleteall'):
            with pytest.raises(ValueError):
                await bot.callback(1, None, 2000, action)
        await bot.send_bottom(1, None, {'screen': 'deleteall', 'text': 'Удалить всё?',
            'rows': [[{'text': '🗑 Да, удалить всё', 'callback_data': 'confirmdeleteall:old'}]]})
        assert not any('Удалить всё' in b['text'] for row in reply_keyboard(bot)['keyboard'] for b in row)
        assert store.note(1, note['id']) == note and bot.archive.directory(note).exists()
        assert not store.folder(1, folder)['removed']
        assert store.all('SELECT * FROM deletion_jobs') == []
        assert not hasattr(bot.archive, 'delete_all') and not hasattr(bot.cleanup, 'begin')
    asyncio.run(run())


def test_telegram_tracks_every_sent_chunk_media_and_upload_without_contents(tmp_path):
    async def run():
        mid = 100
        def handler(request):
            nonlocal mid
            mid += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": mid}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            telegram = Telegram("1:token", client)
            recorded = []
            telegram.on_message = lambda *args: recorded.append(args)
            await telegram.text(1, "private text", 20)
            await telegram.call("sendVoice", chat_id=1, message_thread_id=20, voice="private file id")
            path = tmp_path / "archive.zip"
            path.write_bytes(b"private content")
            await telegram.document(1, path, 20)
            assert recorded == [(1, 101, 20), (1, 102, 20), (1, 103, 20)]
            assert "private" not in json.dumps(recorded)
    asyncio.run(run())
