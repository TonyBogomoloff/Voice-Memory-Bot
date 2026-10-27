"""Telegram connection controls for one-way Obsidian export."""
from .obsidian_access import ObsidianAccess
from .obsidian_settings import ObsidianSettings


class ObsidianMenuMixin:
    async def obsidian_menu(self, owner, thread, scope='all', edit_id=None, *, code=None):
        from .bot import button, keyboard

        folder = self.store.folder(owner, scope) if scope != 'all' else None
        label = 'весь архив' if folder is None else 'папка «' + folder['name'] + '»'
        settings = ObsidianSettings(self.store)
        audio = settings.audio(owner, scope)
        rows = [[button('📤 Экспортировать все папки' if folder is None else '📤 Экспортировать папку',
                        f'zip:{scope}:obsidian')],
                [button('🎙 Экспортировать аудио: ' + ('вкл' if audio else 'выкл'),
                        f'obsidianaudio:{scope}:{0 if audio else 1}')]]
        if folder is not None and settings.override(owner, scope) is not None:
            rows.append([button('↩️ Как в общих настройках', f'obsidianaudio:{scope}:default')])
        text = ('💎 Obsidian\n\nОдна папка — одна заметка. Записи по времени, текст и фото включены всегда.\n'
                'Область: ' + label + '.\nАудио: ' + ('включено.' if audio else 'выключено.'))
        if folder is None:
            text += '\nНастройка аудио применяется к папкам без отдельного выбора. В меню папки можно изменить её.'
        elif settings.override(owner, scope) is None:
            text += '\nАудио — по общим настройкам.'
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
                               back='settings:all' if scope == 'all' else f'foldermenu:{scope}', keep=True, edit_id=edit_id)

    async def obsidian_callback(self, owner, thread, message_id, data):
        parts = data.split(':')
        if parts[0] not in {'obsidian', 'obsidianpair', 'obsidianrevoke', 'obsidianaudio'}:
            return False
        access = ObsidianAccess(self.store)
        if parts[0] == 'obsidianaudio':
            if len(parts) != 3 or parts[2] not in {'0', '1', 'default'}:
                raise ValueError('Открой подключение Obsidian заново')
            ObsidianSettings(self.store).set_audio(owner, parts[1], None if parts[2] == 'default' else parts[2] == '1')
            await self.obsidian_menu(owner, thread, parts[1], message_id)
        elif parts[0] == 'obsidianrevoke':
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
