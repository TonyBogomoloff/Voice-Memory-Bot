"""Blue Tape presentation for Telegram; original text and routing stay intact."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

PACK = json.loads(Path(__file__).with_name('emoji_pack.json').read_text())
IDS = PACK['ids']

def icon_id(name):
    return IDS['menu_list' if name == 'menu' else name]

# Non-whitespace invisible labels survive Telegram's required text and input stripping.
ARROW_LABELS = {'back': '\u2060', 'next': '\u2060\u2060'}

# Unicode stays in message text as a portable fallback; Telegram draws these entities in blue.
GLYPHS = {
    '🎙': 'microphone', '🎤': 'microphone', '🎚': 'recorder', '📼': 'cassette',
    '📁': 'folder', '📂': 'folders', '✅': 'folder_active', '☑': 'check', '✓': 'check',
    '➕': 'add', '＋': 'add', '🔎': 'search', '🔍': 'search', '🤖': 'ai',
    '📦': 'export', '⬇': 'export', '⚙': 'settings', 'ℹ': 'help', '🗑': 'delete',
    '✏': 'edit', '📄': 'text', '📝': 'notes', '📷': 'photo', '📸': 'photo',
    '📹': 'video', '🎥': 'video', '📎': 'attach', '⬅': 'back', '←': 'back', '◀': 'back',
    '➡': 'next', '→': 'next', '▶': 'next', '🏠': 'home', '⌂': 'home', '☰': 'menu',
    '🎛': 'menu', '✖': 'cancel', '❌': 'cancel', '📌': 'pin', '🕘': 'history',
    '🔄': 'retry', '💬': 'chat', '🔒': 'access', '🧠': 'model', '🎵': 'waveform',
    '🎧': 'waveform', '↪': 'move', '✨': 'spark', '✦': 'spark', '🔘': 'toggle',
    '↩': 'undo', '⏹': 'stop', '⚠': 'warning', '🔴': 'record',
}
# Entities require a valid ordinary emoji fallback, not decorative ASCII-like symbols.
ENTITY_FALLBACKS = {'✓': '☑️', '＋': '➕', '←': '⬅️', '→': '➡️', '⌂': '🏠', '☰': '🎛️', '✦': '✨'}
PATTERN = re.compile('[' + re.escape(''.join(GLYPHS)) + ']\ufe0f?')
ACTION_ICONS = {
    'folders': 'folders', 'folder': 'folder', 'foldermenu': 'folder',
    'create': 'add', 'newfor': 'add', 'rnew': 'add', 'notes': 'notes', 'note': 'notes',
    'search': 'search', 'aiopen': 'ai', 'aiask': 'ai', 'aicontinue': 'chat', 'aiview': 'ai',
    'aiconfirm': 'check', 'aicancel': 'cancel', 'ainew': 'chat', 'ailist': 'history',
    'aisettings': 'settings', 'aimodels': 'model', 'aimodel': 'model', 'aiaccessmenu': 'access',
    'aiaccess': 'access', 'aiwake': 'toggle', 'ainames': 'edit', 'aiundo': 'undo',
    'airetry': 'retry', 'aicollapse': 'back', 'export': 'export', 'zip': 'export',
    'exportsettings': 'settings', 'exportformat': 'text', 'settings': 'settings', 'help': 'help',
    'deleteall': 'delete', 'delete': 'delete', 'dropfolder': 'delete',
    'confirmdelete': 'delete', 'confirmdeleteall': 'delete', 'confirmdrop': 'delete',
    'canceldelete': 'cancel', 'canceldrop': 'cancel', 'rcancel': 'cancel', 'rename': 'edit',
    'text': 'text', 'original': 'text', 'audio': 'waveform', 'retry': 'retry',
    'retrydeleteall': 'retry', 'home': 'home', 'options': 'menu', 'pin': 'pin', 'unpin': 'pin',
    'recordmove': 'move', 'pick': 'move', 'choose': 'move', 'place': 'folder', 'rmove': 'folder',
    'menumessagebuttons': 'toggle', 'messagebuttons': 'toggle', 'cancel_record_options': 'cancel',
}
BARE_LABELS = {'add': 'Создать', 'back': 'Назад', 'next': 'Дальше', 'menu': 'Меню',
               'cancel': 'Отмена', 'stop': 'Стоп', 'home': 'Главная'}


def utf16(text):
    return len(text.encode('utf-16-le')) // 2


def semantic_icon(item):
    text = item['text'].strip()
    if text == '📁 Папка':
        return 'settings'
    match = PATTERN.match(text)
    marker = GLYPHS[match.group().replace('\ufe0f', '')] if match else None
    if marker in {'check', 'folder_active', 'back', 'next'}:
        return marker
    action = item.get('callback_data', '').split(':', 1)[0]
    if action in {'menumessagebuttons', 'messagebuttons'}:
        # Callback encodes the next state; the icon shows the current one.
        return 'toggle_off' if item['callback_data'].endswith(':1') else 'toggle'
    lower = text.lower()
    if lower.startswith('включить кнопки') or lower.startswith('кнопки у сообщений: выкл'):
        return 'toggle_off'
    if action in {'audio'} and marker:
        return marker
    if action in ACTION_ICONS:
        return ACTION_ICONS[action]
    if marker:
        return marker
    if text.endswith(('➡️', '▶️')):
        return 'next'
    lower = text.lower()
    for words, name in [
        (('отменить правку',), 'undo'), (('отмена', 'отменить'), 'cancel'),
        (('переместить', 'перенести', 'перенос'), 'move'), (('удалить', 'очист'), 'delete'),
        (('переимен', 'изменить имена'), 'edit'), (('изменить с ии', 'ии'), 'ai'),
        (('кнопки', 'обращение по имени'), 'toggle'), (('доступ',), 'access'),
        (('модель', 'gpt-'), 'model'), (('настройки', 'формат'), 'settings'),
        (('новый разговор',), 'chat'), (('история',), 'history'), (('повтор',), 'retry'),
        (('исходники',), 'cassette'), (('аудио',), 'waveform'), (('pdf', 'markdown', 'текст', 'расшифровка'), 'text'),
        (('экспорт', 'скачать', 'всё:'), 'export'), (('help', 'помощь'), 'help'),
        (('папк',), 'folder'), (('назад',), 'back'), (('главная',), 'home'),
    ]:
        if any(word in lower for word in words):
            return name
    return 'menu'


def button_label(text, name):
    if name in ARROW_LABELS:
        return ARROW_LABELS[name]
    if text == '📁 Папка':
        return 'Настройки'
    # Only the UI's leading marker is removed. A user's emoji in a title is kept.
    match = PATTERN.match(text)
    if match:
        text = text[match.end():].lstrip()
    if text.endswith((' ➡️', ' ▶️')):
        text = text[:-3].rstrip()
    return text or BARE_LABELS.get(name, 'Открыть')


def themed_markup(markup):
    result = deepcopy(markup)
    aliases = {}
    kind = 'keyboard' if 'keyboard' in result else 'inline_keyboard'
    used = set()
    for row in result.get(kind, []):
        for i, original in enumerate(row):
            item = {'text': original} if isinstance(original, str) else dict(original)
            if item.get('icon_custom_emoji_id'):
                continue
            name = semantic_icon(item)
            label = button_label(item['text'], name)
            # Commands typed by hand remain commands, even for a folder called /delete.
            if kind == 'keyboard' and label.startswith('/'):
                label = 'Папка · ' + label
            stem, n = label, 2
            while kind == 'keyboard' and label in used:
                label = stem + '\u200b' * n if name in ARROW_LABELS else f'{stem} · {n}'
                n += 1
            used.add(label)
            if kind == 'keyboard':
                aliases[label] = item['text']
            row[i] = {**item, 'text': label, 'icon_custom_emoji_id': icon_id(name)}
    if result.get('input_field_placeholder', '').startswith('📁 '):
        result['input_field_placeholder'] = result['input_field_placeholder'][2:]
    return result, aliases


def themed_text(text, existing=()):
    """Add emoji entities without disturbing user text, existing offsets, or code blocks."""
    entities = list(deepcopy(existing))
    # Non-emoji symbols in existing text cannot be entities. Keep them unchanged.
    for match in PATTERN.finditer(text):
        glyph = match.group().replace('\ufe0f', '')
        if glyph in ENTITY_FALLBACKS:
            continue
        offset, length = utf16(text[:match.start()]), utf16(match.group())
        if any(e['offset'] < offset + length and offset < e['offset'] + e['length'] for e in entities):
            continue
        entities.append({'type': 'custom_emoji', 'offset': offset, 'length': length,
                         'custom_emoji_id': icon_id(GLYPHS[glyph])})
    return sorted(entities, key=lambda e: e['offset'])


def themed_payload(method, payload):
    if method not in {'sendMessage', 'editMessageText', 'editMessageReplyMarkup', 'sendPhoto',
                      'sendVideo', 'sendAudio', 'sendVoice', 'sendDocument', 'editMessageCaption',
                      'copyMessage', 'sendAnimation', 'sendMediaGroup'}:
        return payload, {}
    result = deepcopy(payload)
    aliases = {}
    if result.get('reply_markup'):
        result['reply_markup'], aliases = themed_markup(result['reply_markup'])
    if not result.get('parse_mode'):
        for field, entity_field in [('text', 'entities'), ('caption', 'caption_entities')]:
            if result.get(field):
                entities = themed_text(result[field], result.get(entity_field) or ())
                if entities:
                    result[entity_field] = entities
    for media in result.get('media', []):
        if media.get('caption') and not media.get('parse_mode'):
            entities = themed_text(media['caption'], media.get('caption_entities') or ())
            if entities:
                media['caption_entities'] = entities
    return result, aliases


class KeyboardAliases:
    """Persist the displayed reply-button labels before sending, including across restarts."""

    def __init__(self, store):
        self.store = store
        with store.transaction():
            store.db.execute('CREATE TABLE IF NOT EXISTS emoji_keyboard_labels ('
                             'owner INTEGER NOT NULL, label TEXT NOT NULL, original TEXT NOT NULL, '
                             'active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(owner,label))')

    def remember(self, owner, aliases):
        with self.store.transaction():
            self.store.db.execute('UPDATE emoji_keyboard_labels SET active=0 WHERE owner=?', (owner,))
            self.store.db.executemany(
                'INSERT INTO emoji_keyboard_labels(owner,label,original,active) VALUES (?,?,?,1) '
                'ON CONFLICT(owner,label) DO UPDATE SET original=excluded.original,active=1',
                [(owner, label, original) for label, original in aliases.items()])

    def resolve(self, owner, text):
        row = self.store.one('SELECT original,active FROM emoji_keyboard_labels WHERE owner=? AND label=?',
                             (owner, text))
        return (row['original'], bool(row['active'])) if row else (text, None)
