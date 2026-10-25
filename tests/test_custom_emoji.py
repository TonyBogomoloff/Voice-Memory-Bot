import asyncio
import json

import httpx
import pytest
from test_commands import bot as bot
from test_commands import receive

from voice_memory_bot.custom_emoji import IDS, KeyboardAliases, themed_markup, themed_payload, themed_text
from voice_memory_bot.providers import ProviderError, Telegram


def test_utf16_entities_preserve_existing_formatting_and_original_text():
    text = '🧪 📁 Работа 🤖'
    bold = {'type': 'bold', 'offset': 6, 'length': 6}
    result = themed_text(text, [bold])
    assert result == [
        {'type': 'custom_emoji', 'offset': 3, 'length': 2, 'custom_emoji_id': IDS['folder']},
        bold,
        {'type': 'custom_emoji', 'offset': 13, 'length': 2, 'custom_emoji_id': IDS['ai']},
    ]
    assert themed_text('🤖', [{'type': 'code', 'offset': 0, 'length': 2}]) == [
        {'type': 'code', 'offset': 0, 'length': 2}]
    assert themed_text('🎙️')[0]['length'] == 3
    payload = {'text': text, 'entities': [bold]}
    transformed, _ = themed_payload('sendMessage', payload)
    assert transformed['text'] == text and payload == {'text': text, 'entities': [bold]}
    assert themed_payload('answerCallbackQuery', payload) == (payload, {})


def test_reply_label_collisions_and_commands_keep_distinct_routes():
    original = {'keyboard': [[{'text': '📁 Меню'}, {'text': '☰ Меню'}, {'text': '📁 /delete'},
                             {'text': '✅ Работа'}, {'text': '＋'}]]}
    result, aliases = themed_markup(original)
    assert aliases == {'Меню': '📁 Меню', 'Меню · 2': '☰ Меню', 'Папка · /delete': '📁 /delete',
                       'Работа': '✅ Работа', 'Создать': '＋'}
    assert result['keyboard'][0][3]['icon_custom_emoji_id'] == IDS['folder_active']
    assert all(b.get('icon_custom_emoji_id') for b in result['keyboard'][0])
    assert original['keyboard'][0][0]['text'] == '📁 Меню'
    again, _ = themed_markup(result)
    assert again == result


def test_inline_actions_and_media_captions_have_custom_icons():
    payload = {'caption': '📷 Фото', 'reply_markup': {'inline_keyboard': [[
        {'text': 'Переместить', 'callback_data': 'pick:a:0'},
        {'text': 'Удалить', 'callback_data': 'delete:a', 'style': 'danger'},
        {'text': 'ИИ', 'callback_data': 'aiask:a'},
    ]]}}
    result, aliases = themed_payload('sendPhoto', payload)
    assert not aliases and result['caption'] == payload['caption']
    assert result['caption_entities'][0]['custom_emoji_id'] == IDS['photo']
    buttons = result['reply_markup']['inline_keyboard'][0]
    assert [b['icon_custom_emoji_id'] for b in buttons] == [IDS['move'], IDS['delete'], IDS['ai']]
    assert buttons[1]['callback_data'] == 'delete:a' and buttons[1]['style'] == 'danger'


def test_explicit_emoji_rejection_retries_once_with_original_payload():
    async def run():
        sent, receipts, remembered = [], [], []
        def handler(request):
            sent.append(json.loads(request.content))
            if len(sent) == 1:
                return httpx.Response(400, json={'ok': False, 'error_code': 400,
                                                'description': 'Bad Request: CUSTOM_EMOJI_ID_INVALID'})
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 10}})
        telegram = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        telegram.on_message = lambda *args: receipts.append(args)
        telegram.on_keyboard = lambda *args: remembered.append(args)
        keys = {'keyboard': [[{'text': '📁 Работа'}]]}
        await telegram.text(1, '📁 Работа', keyboard=keys)
        assert len(sent) == 2 and sent[0]['entities'] and 'entities' not in sent[1]
        assert sent[1]['reply_markup'] == keys
        assert receipts == [(1, 10, None)] and remembered == [(1, {'Работа': '📁 Работа'})]
        await telegram.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['timeout', 'server', 'unrelated_400'])
def test_uncertain_or_unrelated_errors_never_repeat_delivery(failure):
    async def run():
        sent = []
        def handler(request):
            sent.append(request)
            if failure == 'timeout':
                raise httpx.ReadTimeout('lost response')
            code = 500 if failure == 'server' else 400
            return httpx.Response(code, json={'ok': False, 'error_code': code, 'description': 'chat not found'})
        telegram = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with pytest.raises(ProviderError):
            await telegram.text(1, '🤖 Ответ')
        assert len(sent) == 1
        await telegram.close()
    asyncio.run(run())


def test_real_transport_keyboard_taps_survive_restart_and_never_become_notes(bot):
    async def run():
        sent = []
        def handler(request):
            payload = json.loads(request.content)
            sent.append((request.url.path.rsplit('/', 1)[-1], payload))
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 1000 + len(sent)}})
        telegram = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        telegram.on_message = bot.telegram.on_message
        bot.telegram = telegram
        telegram.on_keyboard = bot.emoji_keyboards.remember
        work = bot.store.create_folder(1, 'Работа')
        await receive(bot, '/start', 100)
        keyboard = next(p['reply_markup'] for m, p in reversed(sent) if m == 'sendMessage')
        assert all(b.get('icon_custom_emoji_id') for row in keyboard['keyboard'] for b in row)
        bot.emoji_keyboards = KeyboardAliases(bot.store)
        telegram.on_keyboard = bot.emoji_keyboards.remember
        await receive(bot, 'Работа', 101)
        assert bot.store.current_folder(1) == work['id']
        await receive(bot, 'Работа', 102)
        assert bot.store.notes(1) == []
        await receive(bot, 'Меню', 103)
        assert bot.bottom_state(1)['screen'] == 'home'
        # A delayed tap from the old keyboard must not become a note in the current folder.
        await receive(bot, 'Работа', 104)
        assert bot.store.notes(1) == []
        await receive(bot, 'Настройки', 105)
        assert bot.bottom_state(1)['screen'] == f"foldermenu:{work['id']}"
        await receive(bot, '\u2060', 106)
        assert bot.bottom_state(1)['screen'] == 'root'
        await telegram.close()
    asyncio.run(run())


def test_aliases_are_owner_scoped_and_obsolete_labels_are_marked(bot):
    bot.emoji_keyboards.remember(1, {'Работа': '📁 Работа'})
    assert KeyboardAliases(bot.store).resolve(1, 'Работа') == ('📁 Работа', True)
    assert bot.emoji_keyboards.resolve(2, 'Работа') == ('Работа', None)
    bot.emoji_keyboards.remember(1, {'Help': 'ℹ️ Help'})
    assert bot.emoji_keyboards.resolve(1, 'Работа') == ('📁 Работа', False)


def test_document_caption_uses_entities_without_changing_export_bytes(tmp_path):
    async def run():
        path = tmp_path / 'archive.zip'
        path.write_bytes(b'unchanged export')
        sent = []
        def handler(request):
            sent.append(request.content)
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 10}})
        telegram = Telegram('123:test', httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        await telegram.document(1, path, caption='📦 Архив')
        assert len(sent) == 1 and b'unchanged export' in sent[0]
        assert b'caption_entities' in sent[0] and IDS['export'].encode() in sent[0]
        await telegram.close()
    asyncio.run(run())


def test_icon_only_navigation_keeps_distinct_routes():
    from voice_memory_bot.custom_emoji import IDS, themed_markup
    markup, aliases = themed_markup({'keyboard': [[{'text': '⬅️ Назад'}, {'text': 'Страница ▶️'}],
                                                  [{'text': '📁 Папка'}, {'text': '☰ Меню'}]]})
    left, right = markup['keyboard'][0]
    assert left['text'].strip() and right['text'].strip()
    assert left['text'] != right['text']
    assert all(c in '\u2060\u200b' for c in left['text'] + right['text'])
    assert aliases[left['text']] == '⬅️ Назад'
    assert aliases[right['text']] == 'Страница ▶️'
    settings, menu = markup['keyboard'][1]
    assert settings['text'] == 'Настройки'
    assert settings['icon_custom_emoji_id'] == IDS['settings']
    assert aliases['Настройки'] == '📁 Папка'
    assert menu['icon_custom_emoji_id'] == IDS['menu_list']
