import asyncio

import pytest
from test_commands import bot as bot
from test_commands import receive

from voice_memory_bot.folder_view import RENEW_AFTER
from voice_memory_bot.providers import ProviderError, TelegramError


def saved(bot, folder, text, mid):
    note, _ = bot.store.add_note(1, folder, {'message_id': mid}, 'text', transcript=text)
    bot.store.update_note(1, note['id'], state='ready', delivery='sent', transcript_delivery='sent', title=text)
    bot.cleanup.remember(1, mid)
    return note


def test_switch_renders_only_selected_folder_and_preserves_archive(bot, tmp_path):
    async def run():
        inbox = bot.store.ensure_user(1)
        work = bot.store.create_folder(1, 'Работа')['id']
        first = saved(bot, inbox, 'Текст первой папки', 1001)
        second = saved(bot, work, 'Текст второй папки', 1002)
        original = tmp_path / 'notes' / 'original.ogg'
        original.parent.mkdir(exist_ok=True)
        original.write_bytes(b'original audio')
        before = bot.store.all('SELECT * FROM notes ORDER BY id')
        bot.cleanup.remember(2, 2001)
        bot.cleanup.remember(1, 3001, 99)
        await bot.show_notes(1, None, work)
        deleted = {p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage'}
        assert {1001, 1002, 3001} <= deleted and 2001 not in deleted
        texts = [p['text'] for m, p in bot.telegram.sent if m == 'sendMessage']
        assert 'Текст второй папки' in texts and 'Текст первой папки' not in texts
        bot.telegram.sent.clear()
        await bot.show_notes(1, None, inbox)
        assert any(p.get('text') == 'Текст первой папки' for _, p in bot.telegram.sent)
        assert bot.store.all('SELECT * FROM notes ORDER BY id') == before
        assert original.read_bytes() == b'original audio'
        await receive(bot, 'Новая надиктовка', 4001)
        assert bot.store.notes(1)[0]['folder_id'] == inbox
        assert bot.store.note(1, first['id']) and bot.store.note(1, second['id'])
    asyncio.run(run())


def test_renewal_uses_oldest_send_not_recent_activity_and_survives_restart(bot):
    async def run():
        inbox = bot.store.ensure_user(1)
        saved(bot, inbox, 'Запись для обновления', 1001)
        await bot.show_notes(1, None, inbox)
        with bot.store.db:
            bot.store.db.execute('UPDATE telegram_messages SET sent_at=1000')
        bot.cleanup.remember(1, 9001, sent_at=1000 + RENEW_AFTER - 10)
        # Editing/receiving the same ID must not extend the original deletion deadline.
        old = bot.store.one('SELECT message_id FROM telegram_messages WHERE sent_at=1000')['message_id']
        bot.cleanup.remember(1, old)
        bot.init_folder_view()
        bot.telegram.sent.clear()
        await bot.refresh_due_views(now=1000 + RENEW_AFTER - 1)
        assert bot.telegram.sent == []
        await bot.refresh_due_views(now=1000 + RENEW_AFTER)
        assert any(m == 'deleteMessage' and p['message_id'] == old for m, p in bot.telegram.sent)
        assert bot.store.one('SELECT dirty FROM folder_views WHERE owner=1')['dirty'] == 0
        bot.telegram.sent.clear()
        await bot.refresh_due_views(now=1000 + RENEW_AFTER + 1)
        assert bot.telegram.sent == []
    asyncio.run(run())


def test_processing_defers_deletion_but_selects_destination_immediately(bot):
    async def run():
        inbox = bot.store.ensure_user(1)
        work = bot.store.create_folder(1, 'Работа')['id']
        await receive(bot, 'Ещё сохраняется', 1001)
        note = bot.store.notes(1)[0]
        bot.telegram.sent.clear()
        await bot.show_notes(1, None, work)
        assert bot.store.current_folder(1) == work
        assert not any(m == 'deleteMessage' for m, _ in bot.telegram.sent)
        assert bot.store.note(1, note['id'])['folder_id'] == inbox
        bot.store.update_note(1, note['id'], state='ready', delivery='sent', transcript_delivery='sent')
        await bot.refresh_due_views()
        assert any(m == 'deleteMessage' for m, _ in bot.telegram.sent)
        assert bot.store.one('SELECT dirty FROM folder_views WHERE owner=1')['dirty'] == 0
    asyncio.run(run())


def test_failed_delete_remains_retryable_and_expired_id_stays_available_to_delete_all(bot):
    async def run():
        inbox = bot.store.ensure_user(1)
        bot.cleanup.remember(1, 1001, sent_at=1)
        call = bot.telegram.call
        error = ProviderError('telegram_transport_unknown')

        async def fail(method, **payload):
            if method == 'deleteMessage' and payload['message_id'] == 1001:
                raise error
            return await call(method, **payload)

        bot.telegram.call = fail
        with pytest.raises(ProviderError):
            await bot.show_notes(1, None, inbox)
        assert bot.store.one('SELECT dirty FROM folder_views WHERE owner=1')['dirty'] == 1
        assert bot.store.one('SELECT * FROM telegram_messages WHERE message_id=1001')
        error = TelegramError(400, 'deleteMessage', "message can't be deleted")
        await bot.refresh_due_views()
        assert bot.store.one('SELECT * FROM telegram_messages WHERE message_id=1001')['view_blocked'] == 1
        assert bot.store.one('SELECT dirty FROM folder_views WHERE owner=1')['dirty'] == 0
        bot.cleanup.begin(1)
        await bot.refresh_due_views()
        assert bot.store.all('SELECT * FROM folder_views') == []
        assert 1001 in [t['identity'] for t in bot.cleanup.targets(1, 'message')]
    asyncio.run(run())


def test_rate_limit_retries_only_refused_send_and_keeps_unsaved_source(bot, monkeypatch):
    async def run():
        inbox = bot.store.ensure_user(1)
        note, _ = bot.store.add_note(1, inbox, {'message_id': 1001}, 'voice', 'file', 'ogg', 12, 1)
        bot.store.update_note(1, note['id'], state='asr_failed', delivery='sent', transcript_delivery='unknown')
        bot.cleanup.remember(1, 1001)
        call = bot.telegram.call
        refused = False

        async def limited(method, **payload):
            nonlocal refused
            if method == 'sendMessage' and not refused:
                refused = True
                raise TelegramError(429, method, 'Too Many Requests')
            return await call(method, **payload)

        async def no_wait(seconds):
            pass

        monkeypatch.setattr(asyncio, 'sleep', no_wait)
        bot.telegram.call = limited
        await bot.show_notes(1, None, inbox)
        assert refused
        assert not any(m == 'deleteMessage' and p['message_id'] == 1001 for m, p in bot.telegram.sent)
        assert bot.store.one('SELECT dirty FROM folder_views WHERE owner=1')['dirty'] == 0
        assert bot.store.one('SELECT * FROM telegram_messages WHERE message_id=1001')
        bot.store.set_action(1, 'create')
        with bot.store.db:
            bot.store.db.execute('UPDATE telegram_messages SET sent_at=1')
        await bot.refresh_due_views()
        assert bot.store.action(1) == 'create'
    asyncio.run(run())


def test_daily_cleanup_restores_settings_after_restart_and_cleans_all_known_threads(bot):
    async def run():
        assert RENEW_AFTER == 24 * 3600
        inbox = bot.store.ensure_user(1)
        old_note = saved(bot, inbox, 'Архивная запись', 1001)
        await bot.show_notes(1, None, inbox)
        await receive(bot, '/settings', 2001)
        bot.cleanup.remember(1, 3001, 99, sent_at=1000)
        bot.cleanup.remember(2, 3002, 99, sent_at=1000)
        bot.messages.record(old_note, 3003, 98, 'source')
        before = bot.store.all('SELECT * FROM notes')
        bot.init_folder_view()
        bot.telegram.sent.clear()
        await bot.refresh_due_views(now=1000 + RENEW_AFTER - 1)
        assert not bot.telegram.sent
        await bot.refresh_due_views(now=1000 + RENEW_AFTER)
        deleted = {p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage'}
        assert {3001, 3003} <= deleted and 3002 not in deleted
        assert bot.telegram.sent[-1][1]['text'] == '⚙️ Настройки'
        assert not any(p.get('text') == 'Архивная запись' for _, p in bot.telegram.sent)
        assert bot.store.all('SELECT * FROM notes') == before
        assert bot.store.current_folder(1) == inbox
        assert not any(m == 'deleteForumTopic' for m, _ in bot.telegram.sent)
    asyncio.run(run())


def test_settings_without_prior_folder_visit_and_return_to_folder(bot):
    async def run():
        await receive(bot, '/settings', 1001)
        assert bot.store.one('SELECT screen FROM folder_views WHERE owner=1')['screen']
        with bot.store.db:
            bot.store.db.execute('UPDATE telegram_messages SET sent_at=1')
        await bot.refresh_due_views()
        assert bot.telegram.sent[-1][1]['text'] == '⚙️ Настройки'
        inbox = bot.store.current_folder(1)
        await bot.show_notes(1, None, inbox)
        assert bot.store.one('SELECT screen FROM folder_views WHERE owner=1')['screen'] is None
        bot.telegram.sent.clear()
        with bot.store.db:
            bot.store.db.execute('UPDATE telegram_messages SET sent_at=1')
        await bot.refresh_due_views()
        assert not bot.telegram.sent  # An empty folder has no header to renew.
        await bot.settings_menu(1, None)
        bot.cleanup.begin(1)
        assert bot.store.all('SELECT * FROM folder_views WHERE owner=1') == []
    asyncio.run(run())


def test_old_topic_cleanup_does_not_block_switch_and_clears_before_header(bot):
    async def run():
        inbox = bot.store.ensure_user(1)
        work = bot.store.create_folder(1, 'Работа')['id']
        saved(bot, inbox, 'Первая', 1001)
        saved(bot, work, 'Вторая', 1002)
        bot.cleanup.remember(1, 3001, 99)
        with bot.store.db:
            bot.store.db.execute('INSERT INTO deletion_jobs VALUES (1,0)')
            bot.store.db.execute("INSERT INTO deletion_targets VALUES (1,'topic',99,NULL)")
        await bot.show_notes(1, None, work)
        assert bot.telegram.sent[0][0] == 'deleteMessage'
        header = next(p for m, p in bot.telegram.sent if m == 'sendMessage')
        assert '✓ 📌 Работа' in str(header)
        deleted = {p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage'}
        assert {1001, 1002, 3001} <= deleted
        assert any(p.get('text') == 'Вторая' for _, p in bot.telegram.sent)
        assert bot.store.one('SELECT dirty FROM folder_views WHERE owner=1')['dirty'] == 0
        await bot.settings_menu(1, None)
        assert bot.store.one('SELECT screen FROM folder_views WHERE owner=1')['screen']
    asyncio.run(run())


def test_switch_has_all_records_and_no_persistent_header_or_menu(bot):
    async def run():
        old = bot.store.ensure_user(1)
        work = bot.store.create_folder(1, 'Работа')['id']
        await bot.show_notes(1, None, old)
        previous = {r['message_id'] for r in bot.store.all('SELECT * FROM telegram_messages')}
        for i in range(10):
            saved(bot, work, f'Запись {i}', 1000 + i)
        bot.telegram.sent.clear()
        await receive(bot, '📌 Работа', 2000)
        sent = [p for m, p in bot.telegram.sent if m == 'sendMessage']
        assert len(sent) == 11
        assert sent[0]['text'] == '…'
        assert bot.telegram.sent[next(i for i, (m, _) in enumerate(bot.telegram.sent)
                                      if m == 'sendMessage') + 1][0] == 'deleteMessage'
        assert all('Входящие' not in p['text'] for p in sent)
        assert {p['text'] for p in sent[1:]} == {f'Запись {i}' for i in range(10)}
        for p in sent[1:]:
            rows = p['keyboard']['inline_keyboard']
            assert len(rows) == len(rows[0]) == 1
            assert rows[0][0]['text'] == 'Опции'
        deleted = {p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage'}
        assert previous <= deleted
        await bot.show_main_menu(1)
        await bot.callback(1, None, 5000, f'foldermenu:{work}')
        assert 'Удалить папку' in str(bot.telegram.sent[-1])
    asyncio.run(run())


def test_switch_keeps_latest_user_command_including_deferred_render_and_restart(bot):
    async def run():
        inbox = bot.store.ensure_user(1)
        work = bot.store.create_folder(1, 'Работа')['id']
        await receive(bot, '📌 Работа', 7001)
        assert not any(m == 'deleteMessage' and p['message_id'] == 7001 for m, p in bot.telegram.sent)
        note = saved(bot, work, 'Обработка', 8001)
        bot.store.update_note(1, note['id'], state='processing')
        await receive(bot, '📌 Входящие', 7002)
        assert bot.store.current_folder(1) == inbox
        bot.init_folder_view()
        bot.store.update_note(1, note['id'], state='ready')
        bot.telegram.sent.clear()
        await bot.refresh_due_views()
        deleted = {p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage'}
        assert 7001 in deleted and 7002 not in deleted
        assert bot.store.one('SELECT * FROM telegram_messages WHERE message_id=7002')
        with bot.store.db:
            bot.store.db.execute('UPDATE telegram_messages SET sent_at=1 WHERE message_id=7002')
        bot.telegram.sent.clear()
        await bot.refresh_due_views()
        assert not bot.telegram.sent  # The retained command must not trigger an endless renewal loop.
        await receive(bot, '📌 Работа', 7003)
        deleted = {p['message_id'] for m, p in bot.telegram.sent if m == 'deleteMessage'}
        assert 7002 in deleted and 7003 not in deleted
    asyncio.run(run())
