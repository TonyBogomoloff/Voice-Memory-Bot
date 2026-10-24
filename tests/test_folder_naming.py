import asyncio
from types import SimpleNamespace

import pytest
from test_commands import receive
from test_mvp import make_bot, receive_voice

from voice_memory_bot.config import Limits
from voice_memory_bot.providers import ProviderError
from voice_memory_bot.store import Store


def test_first_folder_voice_name_statuses_keyboard_and_original_survive(tmp_path):
    async def run():
        store = Store(tmp_path / 'db', Limits())
        bot = make_bot(tmp_path, store)
        try:
            await receive(bot, '/start')
            assert store.folders(1) == [] and store.action(1) == 'create'
            assert 'первую папку' in bot.telegram.sent[-1][1]['text']
            assert bot.telegram.sent[-1][1]['keyboard'] == {'remove_keyboard': True}

            async def transcribe(path):
                assert path.exists()
                return 'Давай назовём папку Test 2'

            async def extract(text):
                assert text == 'Давай назовём папку Test 2'
                return 'Test 2'

            bot.groq.transcribe = transcribe
            bot.folder_namer = SimpleNamespace(folder_name=extract)
            await receive_voice(bot, 1001, None)
            assert [f['name'] for f in store.folders(1)] == ['Test 2']
            assert store.action(1) is None and store.notes(1) == []
            assert any('Папка «Test 2» создана' in p.get('text', '') for _, p in bot.telegram.sent)
            assert any('✓ 📌 Test 2' in str(p.get('keyboard')) for _, p in bot.telegram.sent)
            changes = [p for m, p in bot.telegram.sent if m == 'editMessageText']
            assert [p['text'] for p in changes] == ['✨ Выделяю название папки…', '📁 Создаю папку «Test 2»…']
            assert changes[0]['message_id'] == changes[1]['message_id']
            assert (bot.archive.root / '1/folder-names/1001/source.ogg').is_file()
            assert (bot.archive.root / '1/folder-names/1001/transcript.txt').read_text() == 'Давай назовём папку Test 2'
            await bot.create_folder_from_voice(1, None, {'message_id': 1001})
            assert len(store.folders(1)) == 1
            store.delete_folder(1, store.current_folder(1))
            assert store.folders(1) == [] and store.current_folder(1) is None
            await receive(bot, '/start', 1002)
            assert 'первую папку' in bot.telegram.sent[-1][1]['text']
            await receive(bot, 'Снова', 1003)
            assert [f['name'] for f in store.folders(1)] == ['Снова']
        finally:
            store.close()
    asyncio.run(run())


def test_inbox_and_last_folder_can_be_removed_while_archive_remains_searchable(tmp_path):
    async def run():
        store = Store(tmp_path / 'db', Limits())
        bot = make_bot(tmp_path, store)
        try:
            inbox = store.ensure_user(1)
            note, _ = store.add_note(1, inbox, {'message_id': 1234}, 'text', transcript='Сохранённый текст')
            store.update_note(1, note['id'], state='ready', delivery='sent', transcript_delivery='sent')
            bot.archive.persist_text(store.note(1, note['id']), store.folder(1, inbox))
            await bot.callback(1, None, 500, 'dropfolder:' + inbox)
            token = bot.folder_delete_confirmations[1][0]
            await bot.callback(1, None, 500, 'confirmdrop:' + token)
            assert store.folders(1) == [] and store.current_folder(1) is None
            assert 'первую папку' in bot.telegram.sent[-1][1]['text']
            assert store.notes(1, query='Сохранённый')[0]['id'] == note['id']
            assert len(store.export_notes(1)) == 1
            assert store.folders(1, include_removed=True)[0]['id'] == inbox
            parts = bot.archive.export(store.export_notes(1), store.folders(1, include_removed=True),
                                       tmp_path / 'export', False, 45_000_000, text_format='markdown')
            assert parts and parts[0].is_file()
            assert store.all('PRAGMA foreign_key_check') == []
            with pytest.raises(ValueError, match='удалена'):
                await bot.show_notes(1, None, inbox)
            await receive(bot, '/new Входящие', 2000)
            assert len(store.folders(1)) == 1 and store.current_folder(1) != inbox
        finally:
            store.close()
    asyncio.run(run())


def test_voice_name_failure_keeps_create_prompt_and_does_not_create_note(tmp_path):
    async def run():
        store = Store(tmp_path / 'db', Limits())
        bot = make_bot(tmp_path, store)
        try:
            async def unavailable(text):
                raise ProviderError('codex_folder_name_failed')
            bot.folder_namer = SimpleNamespace(folder_name=unavailable)
            await receive(bot, '/start')
            await receive_voice(bot, 1001, None)
            assert store.folders(1) == [] and store.notes(1) == []
            assert store.action(1) == 'create'
            assert 'текстом' in bot.telegram.sent[-1][1]['text']
            assert store.one('SELECT transcript FROM folder_name_inputs')['transcript']
            await receive(bot, 'Название вручную', 1002)
            assert [f['name'] for f in store.folders(1)] == ['Название вручную']
        finally:
            store.close()
    asyncio.run(run())
