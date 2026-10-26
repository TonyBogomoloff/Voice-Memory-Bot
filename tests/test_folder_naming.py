import asyncio
from types import SimpleNamespace

import pytest
from support.base import make_bot, receive_voice
from support.commands import receive

from voice_memory_bot.config import Limits
from voice_memory_bot.providers import ProviderError
from voice_memory_bot.store import Store


def last_message(bot):
    return next(p for m, p in reversed(bot.telegram.sent) if m == "sendMessage")


def test_first_folder_voice_name_statuses_keyboard_and_original_survive(tmp_path):
    async def run():
        store = Store(tmp_path / 'db', Limits())
        bot = make_bot(tmp_path, store)
        try:
            await receive(bot, '/start')
            assert store.folders(1) == [] and store.action(1) == 'create'
            assert 'первую папку' in last_message(bot)['text']
            assert last_message(bot)['keyboard']['is_persistent']

            async def transcribe(path, *, prompt=None):
                assert path.exists() and 'папок' in prompt
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
            assert any('✅ Test 2' in str(p.get('keyboard')) for _, p in bot.telegram.sent)
            changes = [p for m, p in bot.telegram.sent if m == 'sendMessage' and p['text'].startswith(('✨ ', '📁 Создаю'))]
            assert [p['text'] for p in changes] == ['✨ Выделяю название папки…', '📁 Создаю папку «Test 2»…']
            assert all('keyboard' in p['keyboard'] for p in changes)
            assert (bot.archive.root / '1/folder-names/1001/source.ogg').is_file()
            assert (bot.archive.root / '1/folder-names/1001/transcript.txt').read_text() == 'Давай назовём папку Test 2'
            await bot.create_folder_from_voice(1, None, {'message_id': 1001})
            assert len(store.folders(1)) == 1
            store.delete_folder(1, store.current_folder(1))
            assert store.folders(1) == [] and store.current_folder(1) is None
            await receive(bot, '/start', 1002)
            assert 'первую папку' in last_message(bot)['text']
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
            assert 'первую папку' in last_message(bot)['text']
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
            assert 'текстом' in last_message(bot)['text']
            assert store.one('SELECT transcript FROM folder_name_inputs')['transcript']
            await receive(bot, 'Название вручную', 1002)
            assert [f['name'] for f in store.folders(1)] == ['Название вручную']
        finally:
            store.close()
    asyncio.run(run())


def test_ambiguous_voice_explains_what_was_heard_with_bottom_cancel(tmp_path):
    async def run():
        store = Store(tmp_path / 'db', Limits())
        bot = make_bot(tmp_path, store)
        try:
            async def ambiguous(text):
                raise ValueError('Скажи одно название папки или напиши его текстом.')
            bot.folder_namer = SimpleNamespace(folder_name=ambiguous)
            await receive(bot, '/new')
            assert last_message(bot)['keyboard']['is_persistent']
            await receive_voice(bot, 1001, None)
            result = last_message(bot)
            assert 'Расслышал:' in result['text'] and 'не удалось' in result['text'].lower()
            assert result['keyboard']['is_persistent']
            assert store.folders(1) == [] and store.action(1) == 'create'
        finally:
            store.close()
    asyncio.run(run())


def test_created_folder_is_not_reported_as_naming_failure(tmp_path):
    async def run():
        store = Store(tmp_path / 'db', Limits())
        bot = make_bot(tmp_path, store)
        try:
            async def extract(text):
                return 'Новая'
            async def unavailable_view(*args, **kwargs):
                raise ProviderError('telegram_429')
            bot.folder_namer = SimpleNamespace(folder_name=extract)
            bot.show_notes = unavailable_view
            await receive(bot, '/new')
            await receive_voice(bot, 1001, None)
            assert [f['name'] for f in store.folders(1)] == ['Новая']
            assert store.action(1) is None
            assert 'Папка «Новая» создана' in last_message(bot)['text']
            assert store.one('SELECT folder_id FROM folder_name_inputs')['folder_id']
        finally:
            store.close()
    asyncio.run(run())


def test_voice_rename_preserves_folder_notes_and_receipt_across_restart(tmp_path):
    async def run():
        store = Store(tmp_path / 'db', Limits())
        bot = make_bot(tmp_path, store)
        try:
            identity = store.ensure_user(1)
            note, _ = store.add_note(1, identity, {'message_id': 2000}, 'text', transcript='Важная запись')
            store.update_note(1, note['id'], state='ready', delivery='sent', transcript_delivery='sent')
            before = store.note(1, note['id'])
            calls = []

            async def transcribe(path, *, prompt=None):
                calls.append('asr')
                return 'Давай переименуем эту папку в Рабочие идеи'

            async def extract(text):
                calls.append('name')
                assert text == 'Давай переименуем эту папку в Рабочие идеи'
                return 'Рабочие идеи'

            bot.groq.transcribe = transcribe
            bot.folder_namer = SimpleNamespace(folder_name=extract)
            await bot.callback(1, None, 500, f'rename:{identity}')
            assert 'голосовое' in last_message(bot)['text']
            await receive_voice(bot, 1001, None)
            assert store.folder(1, identity)['name'] == 'Рабочие идеи'
            assert len(store.folders(1)) == 1 and store.action(1) is None
            assert store.note(1, note['id']) == before
            assert calls == ['asr', 'name']
            store.close()
            store = Store(tmp_path / 'db', Limits())
            store.recover_runtime()
            bot = make_bot(tmp_path, store)
            bot.groq.transcribe = transcribe
            bot.folder_namer = SimpleNamespace(folder_name=extract)
            await bot.create_folder_from_voice(1, None, {'message_id': 1001}, rename_id=identity)
            assert calls == ['asr', 'name']
            assert store.one('SELECT rename_id,folder_id FROM folder_name_inputs') == {
                'rename_id': identity, 'folder_id': identity}
            assert (bot.archive.root / '1/folder-names/1001/source.ogg').is_file()
            assert store.all('PRAGMA foreign_key_check') == []
        finally:
            store.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['ambiguous', 'unavailable', 'duplicate'])
def test_voice_rename_failure_keeps_target_and_allows_text_fallback(tmp_path, failure):
    async def run():
        store = Store(tmp_path / 'db', Limits())
        bot = make_bot(tmp_path, store)
        try:
            identity = store.ensure_user(1)
            store.create_folder(1, 'Занято')
            before = store.folder(1, identity)

            async def extract(text):
                if failure == 'ambiguous':
                    raise ValueError('Скажи одно название')
                if failure == 'unavailable':
                    raise ProviderError('codex_folder_name_failed')
                return 'Занято'

            bot.folder_namer = SimpleNamespace(folder_name=extract)
            await bot.callback(1, None, 500, f'rename:{identity}')
            await receive_voice(bot, 1001, None)
            assert store.folder(1, identity) == before
            assert store.action(1) == f'rename:{identity}'
            assert store.notes(1) == [] and len(store.folders(1)) == 2
            await receive(bot, 'Название вручную', 1002)
            assert store.folder(1, identity)['name'] == 'Название вручную'
            assert store.action(1) is None
        finally:
            store.close()
    asyncio.run(run())


@pytest.mark.parametrize("action", ["rename", "create"])
def test_folder_name_can_reuse_obsolete_keyboard_label(tmp_path, action):
    async def run():
        store = Store(tmp_path / 'db', Limits())
        bot = make_bot(tmp_path, store)
        try:
            folder = store.create_folder(1, 'Работа')
            await bot.show_notes(1, None, folder['id'])
            bot.emoji_keyboards.remember(1, {'Работа': '📁 Работа'})
            await bot.callback(1, None, 500, f"rename:{folder['id']}")
            bot.emoji_keyboards.remember(1, {'Отмена': 'Отмена'})
            await receive(bot, 'Проекты', 1001)
            assert store.folder(1, folder['id'])['name'] == 'Проекты'
            if action == 'rename':
                await bot.callback(1, None, 501, f"rename:{folder['id']}")
            else:
                await receive(bot, '/new', 1002)
            await receive(bot, 'Работа', 1003)
            assert store.action(1) is None
            assert any(f['name'] == 'Работа' for f in store.folders(1))
            assert len(store.folders(1)) == (1 if action == 'rename' else 2)
            assert store.notes(1) == []
            assert not any('кнопка устарела' in p.get('text', '') for _, p in bot.telegram.sent)
            await bot.callback(1, None, 502, f"rename:{folder['id']}")
            await receive(bot, '⬅️ Назад', 1004)
            assert store.action(1) is None
            assert not any(f['name'] == 'Отмена' for f in store.folders(1))
        finally:
            store.close()
    asyncio.run(run())
