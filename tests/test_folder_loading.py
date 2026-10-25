import asyncio

import pytest
from test_commands import bot as bot
from test_folder_view import saved

from voice_memory_bot.providers import ProviderError


@pytest.mark.parametrize('fail', [False, True])
def test_loading_lives_until_delivery_finishes(bot, fail):
    async def run():
        folder = bot.store.current_folder(1)
        saved(bot, folder, 'Последняя запись', 1001)
        original = bot.telegram.text
        loading_id = None

        async def text(owner, content, *args, **kwargs):
            nonlocal loading_id
            if content == 'Последняя запись':
                assert loading_id is not None
                assert not any(m == 'deleteMessage' and p['message_id'] == loading_id
                               for m, p in bot.telegram.sent)
                if fail:
                    raise ProviderError('telegram_transport_unknown')
            result = await original(owner, content, *args, **kwargs)
            if content == 'Загружаю папку…':
                loading_id = result['message_id']
            return result

        bot.telegram.text = text
        if fail:
            with pytest.raises(ProviderError):
                await bot.show_notes(1, None, folder)
        else:
            await bot.show_notes(1, None, folder)
        assert bot.telegram.sent[-1] == ('deleteMessage', {'chat_id': 1, 'message_id': loading_id})
        assert bot.store.one('SELECT dirty FROM folder_views WHERE owner=1')['dirty'] == int(fail)
        assert 1 not in bot.view_rendering
    asyncio.run(run())


def test_only_selected_folder_has_primary_style(bot):
    other = bot.store.create_folder(1, 'Работа')['id']
    for selected in [bot.store.current_folder(1), other]:
        bot.store.select(1, selected)
        buttons = [b for row in bot.folder_keyboard(1)['keyboard'] for b in row]
        styled = [b for b in buttons if b.get('style') == 'primary']
        assert [b['text'] for b in styled] == ['✅ ' + bot.store.folder(1, selected)['name']]


@pytest.mark.parametrize("fail", [False, True])
def test_create_progress_is_visible_during_cleanup_and_removed(bot, fail):
    async def run():
        original = bot.clear_folder_view
        progress_id = None

        original_text = bot.telegram.text
        async def text(owner, content, *args, **kwargs):
            nonlocal progress_id
            result = await original_text(owner, content, *args, **kwargs)
            if content == "Создаю папку…":
                progress_id = result['message_id']
            return result
        bot.telegram.text = text

        async def clear(*args, **kwargs):
            assert progress_id is not None and progress_id in kwargs["keep_ids"]
            if fail:
                raise ProviderError("telegram_transport_unknown")
            return await original(*args, **kwargs)

        bot.clear_folder_view = clear
        if fail:
            with pytest.raises(ProviderError):
                await bot.create_folder(1, None, "Новая папка")
        else:
            await bot.create_folder(1, None, "Новая папка")
        assert bot.telegram.sent[-1] == ("deleteMessage", {"chat_id": 1, "message_id": progress_id})
        assert any(f["name"] == "Новая папка" for f in bot.store.folders(1))
    asyncio.run(run())



def test_new_folder_keyboard_precedes_slow_cleanup_and_survives_it(bot, caplog):
    async def run():
        caplog.set_level('INFO', logger='voice_memory_bot.folder_view')
        folder = bot.store.create_folder(1, 'Секретное название')['id']
        entered, release = asyncio.Event(), asyncio.Event()
        original = bot.clear_folder_view
        async def clear(*args, **kwargs):
            header = bot.store.one('SELECT message_id FROM bottom_menus WHERE owner=1')['message_id']
            assert header in kwargs['keep_ids']
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        bot.clear_folder_view = clear
        task = asyncio.create_task(bot.show_notes(1, None, folder))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert any('Секретное название' in str(p.get('keyboard')) for m, p in bot.telegram.sent if m == 'sendMessage')
            release.set()
            await task
            header = bot.store.one('SELECT message_id FROM bottom_menus WHERE owner=1')['message_id']
            assert bot.store.one('SELECT 1 FROM telegram_messages WHERE owner=1 AND message_id=?', (header,))
            assert 'cleanup_ms=' in caplog.text and 'total_ms=' in caplog.text
            assert 'Секретное название' not in caplog.text
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_processing_completion_wakes_deferred_folder_without_five_second_wait(bot):
    async def run():
        note = saved(bot, bot.store.current_folder(1), 'Сохраняется', 1001)
        bot.store.update_note(1, note['id'], state='queued', delivery='pending')
        folder = bot.store.create_folder(1, 'Работа')['id']
        await bot.show_notes(1, None, folder)
        entered, rendered = asyncio.Event(), asyncio.Event()
        original = bot.refresh_due_views
        async def refresh():
            await original()
            entered.set()
            if not bot.store.one('SELECT dirty FROM folder_views WHERE owner=1')['dirty']:
                rendered.set()
        bot.refresh_due_views = refresh
        async def process(item):
            bot.store.update_note(1, item['id'], state='ready', delivery='sent')
        bot.process = process
        view_worker = asyncio.create_task(bot.folder_view_worker())
        processing_worker = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert not rendered.is_set()
            processing_worker = asyncio.create_task(bot.worker())
            await asyncio.wait_for(rendered.wait(), .8)
            assert bot.store.current_folder(1) == folder
        finally:
            view_worker.cancel()
            if processing_worker:
                processing_worker.cancel()
            await asyncio.gather(view_worker, *([processing_worker] if processing_worker else []), return_exceptions=True)
    asyncio.run(run())
