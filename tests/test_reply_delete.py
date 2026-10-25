import asyncio

import pytest
from test_commands import bot as bot
from test_commands import receive
from test_folder_shortcuts import labels


@pytest.mark.parametrize('target', [100, 200, None, 300])
def test_reply_minus_targets_only_owned_record_and_requires_confirmation(bot, target):
    async def run():
        await receive(bot, 'Сохранённая заметка', 100)
        note = bot.store.notes(1)[0]
        bot.messages.record(note, 200, None, 'transcript')
        bot.store.add_note(2, bot.store.ensure_user(2), {'message_id': 300}, 'text', transcript='Чужая')
        bot.store.set_action(1, 'create')
        message = {'message_id': 400, 'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'}, 'text': '➖'}
        if target:
            message['reply_to_message'] = {'message_id': target}
        await bot.handle({'message': message})
        assert len(bot.store.notes(1)) == 1
        assert bot.store.note(1, note['id'])
        sent = [p for m, p in bot.telegram.sent if m == 'sendMessage']
        confirmation = f"confirmarchive:{note['id']}"
        assert (confirmation in str(sent[-1])) == (target in {100, 200})
        if target in {100, 200}:
            bot.telegram.sent.clear()
            await bot.callback(1, None, 500, confirmation)
            assert bot.store.notes(1) == []
            assert len(bot.store.notes(2)) == 1
            assert not any(m == 'deleteMessage' and p['message_id'] in {100, 200, 300}
                           for m, p in bot.telegram.sent)
    asyncio.run(run())


def test_minus_present_on_home_and_settings(bot):
    async def run():
        for command in ['/start', '/settings', '/folders']:
            await receive(bot, command)
            assert labels(bot).count('➖') == 1
    asyncio.run(run())
