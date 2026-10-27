"""Build a synthetic, deterministic ZIP fixture; never open production data."""
import hashlib
import sys
from pathlib import Path

from voice_memory_bot.exports import ExportSnapshot
from voice_memory_bot.obsidian_export import build
from voice_memory_bot.obsidian_folders import build as build_folders
from voice_memory_bot.originals import Originals


def main():
    output = Path(sys.argv[1])
    originals = Originals(output / 'originals')
    folder = {'id': 'fedcba9876543210', 'owner': 1, 'name': 'Тестовый проект'}
    media = b'synthetic-original-audio' * 150
    voice = {'id': '0123456789abcdef', 'owner': 1, 'folder_id': folder['id'],
             'title': 'Голосовая идея', 'created_at': 1793016000, 'kind': 'voice', 'extension': 'ogg',
             'file_id': 'SYNTHETIC_SECRET_NOT_FOR_EXPORT', 'audio_bytes': len(media),
             'sha256': hashlib.sha256(media).hexdigest(), 'transcript': '  Исходная расшифровка\nбез потерь.  ',
             'content': 'Исправленная **идея**', 'revision': 2}
    text = {**voice, 'id': 'aaaaaaaaaaaaaaaa', 'title': 'Текст', 'kind': 'text', 'extension': 'txt',
            'file_id': None, 'audio_bytes': 0, 'sha256': None, 'content': None,
            'transcript': '[[Ссылка]]\n- [ ] Задача'}
    path = originals.audio(voice)
    path.parent.mkdir(parents=True)
    path.write_bytes(media)
    build(ExportSnapshot.capture([voice, text], [folder]), originals, output / 'zip', True, 5200)
    photos = []
    for number in range(12):
        data = f'synthetic-photo-{number}'.encode() * 60
        photo = {**voice, 'id': f'{number + 256:016x}', 'kind': 'photo', 'extension': 'jpg',
                 'created_at': voice['created_at'] + number + 2, 'title': f'Фото {number}',
                 'content': None, 'transcript': f'Подпись {number}', 'sha256': hashlib.sha256(data).hexdigest(),
                 'audio_bytes': len(data), 'state': 'ready'}
        path = originals.audio(photo)
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        photos.append(photo)
    empty = {**folder, 'id': 'bbbbbbbbbbbbbbbb', 'name': 'Пустая папка'}
    build_folders(ExportSnapshot.capture([{**voice, 'state': 'ready'},
                                          {**text, 'state': 'ready', 'created_at': voice['created_at'] + 1}, *photos],
                                         [folder, empty]), originals, output / 'folder-zip', True, 30000)


if __name__ == '__main__':
    main()
