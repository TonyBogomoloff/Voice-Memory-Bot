import asyncio

import pytest
from support.base import make_bot
from support.commands import bot as bot
from support.commands import receive
from support.folders import deleted_ids, saved
from support.menus import labels

from voice_memory_bot.bottom_menu import BACK
from voice_memory_bot.custom_emoji import ARROW_LABELS, icon_id, themed_markup
from voice_memory_bot.store import Store


@pytest.mark.parametrize("size", [10, 20, 50, 100])
def test_folder_pages_cover_archive_keep_photos_and_clamp_stale_page(bot, size):
    async def run():
        assert bot.store.folder_page_size(1) == 20
        bot.store.set_folder_page_size(1, size)
        folder = bot.store.current_folder(1)
        count = size * 2 + 2
        notes = [saved(bot, folder, f'Запись {i}', 1000 + i) for i in range(count)]
        with bot.store.transaction():
            for index, note in enumerate(notes):
                bot.store.db.execute('UPDATE notes SET entry_type=? WHERE id=?',
                    (('note', 'ai', 'ai_history')[index % 3], note['id']))
        bot.store.update_note(1, notes[-2]['id'], audio_bytes=1)
        with bot.store.transaction():
            bot.store.db.execute("UPDATE notes SET kind='photo',file_id='photo-id' WHERE id=?", (notes[-2]['id'],))
        before = bot.store.all('SELECT * FROM notes ORDER BY id')
        await bot.show_notes(1, None, folder)
        assert bot.store.one('SELECT page FROM folder_views WHERE owner=1')['page'] == 2
        assert [p['text'] for m, p in bot.telegram.sent if m == 'sendMessage'
                and p['text'].startswith('Запись ')] == [f'Запись {i}' for i in range(size * 2, count)]
        for page, expected in [(0, range(size)), (1, range(size, size * 2)),
                               (2, range(size * 2, count)), (999, range(size * 2, count))]:
            bot.telegram.sent.clear()
            await bot.callback(1, None, None, f'folder:{folder}:{page}')
            sent = [p for m, p in bot.telegram.sent if m == 'sendMessage']
            assert sent[0]['text'] == f'📁 Входящие (страница {min(page, 2) + 1} из 3)'
            assert [p['text'] for p in sent if p['text'].startswith('Запись ')] == [f'Запись {i}' for i in expected]
            assert sum('Входящие (страница' in p['text'] for p in sent) == 1
            assert not any(p['text'] == '📁 Входящие' for p in sent)
            navigation = sent[0]['keyboard']['inline_keyboard'][0]
            expected_pages = [1] if page == 0 or page >= 2 else [0, 2]
            assert [b['callback_data'] for b in navigation] == [f'folder:{folder}:{p}' for p in expected_pages]
            icons = ['next'] if page == 0 else ['back'] if page >= 2 else ['back', 'next']
            themed, _ = themed_markup(sent[0]['keyboard'])
            assert [b['icon_custom_emoji_id'] for b in themed['inline_keyboard'][0]] == [icon_id(i) for i in icons]
            assert [b['text'] for b in themed['inline_keyboard'][0]] == [ARROW_LABELS[i] for i in icons]
            photos = [p for m, p in bot.telegram.sent if m == 'sendPhoto']
            assert [p['photo'] for p in photos] == (['photo-id'] if page >= 2 else [])
            assert bot.store.one('SELECT page FROM folder_views WHERE owner=1')['page'] == min(page, 2)
            live_ids = {r['message_id'] for r in bot.store.all('SELECT message_id FROM telegram_messages WHERE owner=1')}
            assert not live_ids & deleted_ids(bot)
        await bot.callback(1, None, None, f'folder:{folder}:0')
        await receive(bot, '✅ Входящие', 9000)
        assert bot.store.one('SELECT page FROM folder_views WHERE owner=1')['page'] == 2
        assert bot.store.all('SELECT * FROM notes ORDER BY id') == before
    asyncio.run(run())


def test_default_twenty_and_single_page_has_no_arrows(bot):
    async def run():
        folder = bot.store.current_folder(1)
        for i in range(20):
            saved(bot, folder, f'Запись {i}', 1000 + i)
        await bot.show_notes(1, None, folder)
        sent = [p for m, p in bot.telegram.sent if m == 'sendMessage']
        header = next(p for p in sent if p['text'].startswith('📁 '))
        assert header['text'] == '📁 Входящие (страница 1 из 1)'
        assert header['keyboard']['keyboard']
        assert len(sent) == 22
        assert len([p for p in sent if p['text'].startswith('Запись ')]) == 20
        assert not any('folder:' in str(p.get('keyboard')) for p in sent)
    asyncio.run(run())


def test_page_size_settings_persist_are_owner_scoped_and_keep_back_path(bot, tmp_path):
    async def run():
        await receive(bot, '/settings', 5000)
        await receive(bot, '📝 Записей на странице · 20', 5001)
        assert {'10', '✓ 20', '50', '100'} <= set(labels(bot))
        await receive(bot, '50', 5002)
        assert '✓ 50' in labels(bot)
        await receive(bot, BACK, 5003)
        assert '📝 Записей на странице · 50' in labels(bot)
        reopened = Store(tmp_path / 'archive.sqlite3', bot.store.limits)
        try:
            assert reopened.folder_page_size(1) == 50 and reopened.folder_page_size(2) == 20
            restarted = make_bot(tmp_path, reopened)
            await receive(restarted, '📝 Записей на странице · 50', 5004)
            await receive(restarted, '10', 5005)
            assert reopened.folder_page_size(1) == 10
            assert reopened.export_preferences(1)['folder_page_size'] == 10
            assert reopened.notes(1) == []
            for invalid in ['0', '101', '-1', 'wrong']:
                with pytest.raises(ValueError):
                    await restarted.callback(1, None, None, f'pagesize:{invalid}')
            assert reopened.folder_page_size(1) == 10
        finally:
            reopened.close()
    asyncio.run(run())
