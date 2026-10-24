from __future__ import annotations

import json
import logging

from .providers import ProviderError

log = logging.getLogger(__name__)


class FolderNamingMixin:
    def init_folder_naming(self):
        self.store.db.execute("""CREATE TABLE IF NOT EXISTS folder_name_inputs (
            owner INTEGER NOT NULL, message_id INTEGER NOT NULL, transcript TEXT,
            name TEXT, folder_id TEXT, PRIMARY KEY(owner,message_id)
        )""")
        self.store.db.commit()

    async def ask_first_folder(self, owner, thread=None):
        self.store.register_user(owner)
        self.store.set_action(owner, "create")
        self.folder_keyboards.pop(owner, None)
        return await self.show_bottom(owner, "Как назовём нашу первую папку? Напиши название или пришли голосовое.",
                                      thread, screen="create", back="home", keep=True)

    async def create_folder_from_voice(self, owner, thread, message):
        mid = message['message_id']
        receipt = self.store.one('SELECT * FROM folder_name_inputs WHERE owner=? AND message_id=?', (owner, mid))
        if receipt and receipt['folder_id']:
            return await self.show_notes(owner, thread, receipt['folder_id'])
        kind = next(k for k in ('voice', 'audio', 'video_note') if message.get(k))
        media = message[kind]
        if not 0 < media.get('file_size', 0) <= self.config.limits.max_file_bytes:
            raise ValueError('Пришли голосовое с названием размером до 20 MB.')
        if media.get('duration', 0) > 60:
            raise ValueError('Для названия папки хватит голосового до одной минуты.')
        keys = {'inline_keyboard': []}
        status = await self.show(owner, '🎙 Расшифровываю название…', thread, keys)
        directory = self.archive.root / str(owner) / 'folder-names' / str(mid)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / ('source.ogg' if kind == 'voice' else 'source.mp4')
        phase = 'asr'
        folder = None
        transcript = None
        try:
            transcript = receipt['transcript'] if receipt else None
            if transcript is None:
                if not path.exists():
                    used = self.store.one('SELECT coalesce(sum(audio_bytes),0) AS n FROM notes WHERE owner=?',
                                          (owner,))['n']
                    used += sum(p.stat().st_size for p in (self.archive.root / str(owner) / 'folder-names').rglob('source.*'))
                    available = min(self.config.limits.max_file_bytes, self.config.limits.max_user_storage_bytes - used)
                    if media['file_size'] > available:
                        raise ValueError('В архиве не хватает места. Напиши название текстом.')
                    await self.telegram.download(media['file_id'], path, available)
                duration = await self.probe_duration(path)
                if duration > 60:
                    raise ValueError('Для названия папки хватит голосового до одной минуты.')
                self.store.reserve_asr(owner, duration)
                transcript = await self.groq.transcribe(path, prompt=
                    'Folder names / Названия папок.')
                with self.store.db:
                    self.store.db.execute('INSERT INTO folder_name_inputs(owner,message_id,transcript) VALUES (?,?,?) '
                                          'ON CONFLICT(owner,message_id) DO UPDATE SET transcript=excluded.transcript',
                                          (owner, mid, transcript))
                (directory / 'transcript.txt').write_text(transcript, encoding='utf-8')
            phase = 'name'
            await self.show(owner, '✨ Выделяю название папки…', thread, keys, status['message_id'])
            name = receipt['name'] if receipt and receipt['name'] else await self.folder_namer.folder_name(transcript)
            phase = 'create'
            with self.store.db:
                self.store.db.execute('UPDATE folder_name_inputs SET name=? WHERE owner=? AND message_id=?',
                                      (name, owner, mid))
            await self.show(owner, f'📁 Создаю папку «{name}»…', thread, keys, status['message_id'])
            folder = self.store.create_folder(owner, name)
            with self.store.db:
                self.store.db.execute('UPDATE folder_name_inputs SET name=?,folder_id=? WHERE owner=? AND message_id=?',
                                      (name, folder['id'], owner, mid))
            (directory / 'result.json').write_text(json.dumps({'name': name, 'folder_id': folder['id']},
                                                             ensure_ascii=False), encoding='utf-8')
            self.store.set_action(owner, None)
            await self.show_notes(owner, thread, folder['id'], notice=f'✅ Папка «{name}» создана.')
        except (ProviderError, OSError, ValueError) as exc:
            log.warning('folder name failed owner=%s phase=%s type=%s', owner, phase, type(exc).__name__)
            if folder is not None:
                self.store.set_action(owner, None)
                text = f'Папка «{folder["name"]}» создана. Открой её через «Все папки».'
            elif isinstance(exc, ValueError):
                text = str(exc)
                if phase == 'name' and transcript:
                    text = f'Расслышал: «{transcript[:1000]}».\nНе удалось однозначно выделить название. Напиши его или повтори голосовое.'
            else:
                text = 'Не получилось обработать название. Пришли его текстом или повтори голосовое.'
            await self.show(owner, text, thread, keys, status['message_id'])
