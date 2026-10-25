import asyncio
import json

import pytest
from test_ai import bot as bot
from test_ai import ready_note, step, tool
from test_ai_note_actions import request_for


def test_folder_is_loaded_only_on_demand_with_full_messages(bot):
    source = ready_note(bot, "Выбранное сообщение", message_id=100)
    hidden = [ready_note(bot, "Полный текст " + str(i) + "x" * 500, message_id=200+i) for i in range(15)]
    other_folder = bot.store.create_folder(1, "Другая")
    other = ready_note(bot, "Не включать", message_id=300)
    bot.store.db.execute("UPDATE notes SET folder_id=? WHERE id=?", (other_folder['id'], other['id']))
    foreign = ready_note(bot, "Чужое", owner=2, message_id=400)
    deleted = ready_note(bot, "Удалено", message_id=500)
    bot.store.db.execute("UPDATE notes SET deleted=1 WHERE id=?", (deleted['id'],))
    # Hidden historical AI answers and user requests are messages too.
    bot.store.db.execute("UPDATE notes SET entry_type='ai_history',ai_generated_from=? WHERE id=?",
                         (source['id'], hidden[0]['id']))
    bot.store.db.execute("UPDATE notes SET entry_type='ai' WHERE id=?", (hidden[1]['id'],))
    bot.store.db.commit()
    bot.ai_state.enqueue(1, hidden[1]['id'], 'button')
    bot.ai_state.update_request(1, hidden[1]['id'], state='ready', response='Ответ без отдельной записи')
    request_id = request_for(bot, source)
    request = bot.ai_state.request(1, request_id)
    initial = bot.ai_engine.payload(request, [])
    assert hidden[0]['transcript'] not in json.dumps(initial, ensure_ascii=False)
    bot.assistant.responses = [step('read_folder', folder_id=source['folder_id']), step(message='Сводка')]
    asyncio.run(bot.ai_engine.run(request))
    result = bot.assistant.calls[-1][0]['steps'][0]['result']
    assert result['complete'] and result['count'] == 16
    assert next(n for n in result['notes'] if n['id'] == hidden[1]['id'])['assistant_response'] == 'Ответ без отдельной записи'
    assert {n['id'] for n in result['notes']} == {source['id'], *(n['id'] for n in hidden)}
    assert not {other['id'], foreign['id'], deleted['id'], request_id} & {n['id'] for n in result['notes']}
    assert next(n for n in result['notes'] if n['id'] == hidden[0]['id'])['content'] == hidden[0]['transcript']
    assert next(n for n in result['notes'] if n['id'] == hidden[0]['id'])['role'] == 'assistant'
    assert [n['created_at'] for n in result['notes']] == sorted(n['created_at'] for n in result['notes'])
    assert tool(bot, request_id, step('read_note', note_id=hidden[0]['id']))['id'] == hidden[0]['id']
    # Loading a folder must not expand permission to edit a different selected-note context.
    assert 'error' in tool(bot, request_id, step('update_note', note_id=hidden[2]['id'],
                                                revision=0, title='Changed', content='Changed'))


@pytest.mark.parametrize('access', ['none', 'read', 'write'])
def test_folder_read_checks_access_ownership_and_explicit_folder(bot, access):
    source = ready_note(bot)
    request_id = request_for(bot, source)
    bot.ai_state.set_settings(1, notes_access=access)
    result = tool(bot, request_id, step('read_folder', folder_id=source['folder_id']))
    assert ('error' in result) == (access == 'none')
    assert 'error' in tool(bot, request_id, step('read_folder'))
    assert 'error' in tool(bot, request_id, step('read_folder', folder_id=bot.store.ensure_user(2)))
    empty = bot.store.create_folder(1, 'Пустая')
    if access != 'none':
        result = tool(bot, request_id, step('read_folder', folder_id=empty['id']))
        assert result['complete'] and result['notes'] == [] and result['count'] == 0
