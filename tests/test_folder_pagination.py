import asyncio

from test_commands import bot as bot
from test_folder_view import saved


def test_folder_pages_cover_archive_keep_photos_and_clamp_stale_page(bot):
    async def run():
        folder = bot.store.current_folder(1)
        notes = [saved(bot, folder, f'Запись {i}', 1000 + i) for i in range(18)]
        bot.store.update_note(1, notes[16]['id'], audio_bytes=1)
        with bot.store.db:
            bot.store.db.execute("UPDATE notes SET kind='photo',file_id='photo-id' WHERE id=?", (notes[16]['id'],))
        before = bot.store.all('SELECT * FROM notes ORDER BY id')
        for page, expected in [(0, range(10, 18)), (1, range(2, 10)), (2, range(2)), (999, range(2))]:
            bot.telegram.sent.clear()
            await bot.callback(1, None, 9999, f'folder:{folder}:{page}')
            sent = [p for m, p in bot.telegram.sent if m == 'sendMessage']
            assert [p['text'] for p in sent if p['text'].startswith('Запись ')] == [f'Запись {i}' for i in expected]
            navigation = sent[-1]['keyboard']['inline_keyboard'][0]
            assert {b['callback_data'] for b in navigation} == (
                {f'folder:{folder}:1'} if page == 0 or page >= 2 else {f'folder:{folder}:0', f'folder:{folder}:2'})
            photos = [p for m, p in bot.telegram.sent if m == 'sendPhoto']
            assert [p['photo'] for p in photos] == (['photo-id'] if page == 0 else [])
            assert bot.store.one('SELECT page FROM folder_views WHERE owner=1')['page'] == min(page, 2)
        assert bot.store.all('SELECT * FROM notes ORDER BY id') == before
    asyncio.run(run())
