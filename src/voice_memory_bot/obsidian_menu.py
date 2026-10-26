"""Telegram connection controls for one-way Obsidian export."""
from .obsidian_access import ObsidianAccess


class ObsidianMenuMixin:
    async def obsidian_menu(self, owner, thread, scope='all', edit_id=None, *, code=None):
        from .bot import button, keyboard

        folder = self.store.folder(owner, scope) if scope != 'all' else None
        label = 'весь архив' if folder is None else 'папка «' + folder['name'] + '»'
        rows = [[button('💎 Скачать ZIP для Obsidian', f'zip:{scope}:obsidian')]]
        text = '💎 Obsidian\n\nОтдельная заметка на каждую запись, с исходными файлами.\nОбласть: ' + label + '.'
        if self.config.obsidian_url:
            rows.append([button('🔗 Подключить устройство', f'obsidianpair:{scope}')])
            text += ('\n\nУстанови плагин Voice Memory и введи код подключения. '
                     'Заметки загружаются при открытии Obsidian.\n'
                     'Адрес сервера: ' + self.config.obsidian_url + '\n'
                     'Установка на телефон: https://github.com/TonyBogomoloff/Voice-Memory-Bot/blob/main/plugins/obsidian/README.md\n'
                     'Пакет плагина: ' + self.config.obsidian_url + '/plugin/voice-memory.zip')
            if code:
                text += '\n\nКод на 10 минут, для одного устройства:\n' + code
            for index, device in enumerate(ObsidianAccess(self.store).devices(owner), 1):
                rows.append([button(f"Отключить · {index}. {device['name']}", f"obsidianrevoke:{device['id']}:{scope}")])
        await self.show_bottom(owner, text, thread, keyboard(*rows), screen=f'obsidian:{scope}',
                               back='settings:all' if scope == 'all' else f'foldermenu:{scope}', edit_id=edit_id)

    async def obsidian_callback(self, owner, thread, message_id, data):
        parts = data.split(':')
        if parts[0] not in {'obsidian', 'obsidianpair', 'obsidianrevoke'}:
            return False
        access = ObsidianAccess(self.store)
        if parts[0] == 'obsidianrevoke':
            if len(parts) != 3:
                raise ValueError('Открой подключение Obsidian заново')
            access.revoke(owner, parts[1])
            await self.obsidian_menu(owner, thread, parts[2], message_id)
        else:
            if len(parts) != 2:
                raise ValueError('Открой подключение Obsidian заново')
            code = None
            if parts[0] == 'obsidianpair':
                if not self.config.obsidian_url:
                    raise ValueError('Подключение Obsidian пока недоступно')
                code = access.issue(owner, None if parts[1] == 'all' else parts[1])
            await self.obsidian_menu(owner, thread, parts[1], message_id, code=code)
        return True
