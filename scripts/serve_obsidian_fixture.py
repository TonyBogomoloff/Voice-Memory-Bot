"""Serve synthetic API records on an ephemeral loopback port for plugin integration tests."""
import asyncio
import hashlib
import json
import signal
import tempfile
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

from voice_memory_bot.config import Limits
from voice_memory_bot.obsidian_access import ObsidianAccess
from voice_memory_bot.obsidian_api import ExportAPI
from voice_memory_bot.originals import Originals
from voice_memory_bot.store import Store


async def main():
    with tempfile.TemporaryDirectory(prefix='voice-memory-obsidian-test-') as directory:
        root = Path(directory)
        store = Store(root / 'archive.sqlite3', Limits())
        archive = Originals(root / 'originals')
        for owner, count in ((1, 53), (2, 1)):
            folder = store.ensure_user(owner)
            for message in range(1, count + 1):
                note = store.add_note(owner, folder, {'message_id': message}, kind='text',
                                      transcript=f'Synthetic note {message}')[0]
                store.update_note(owner, note['id'], state='ready' if message != 53 else 'processing')
        pending = store.create_folder(1, 'Pending')['id']
        with store.transaction():
            store.db.execute("UPDATE notes SET folder_id=? WHERE owner=1 AND state='processing'", (pending,))
            store.db.execute('UPDATE notes SET created_at=1793016000+message_id WHERE owner=1')
        for number in range(21):
            store.create_folder(1, f'Empty {number}')
        note = store.add_note(1, store.ensure_user(1), {'message_id': 54}, kind='voice',
                              extension='ogg', file_id='SYNTHETIC_PRIVATE_FILE_ID', transcript='Голосовая идея')[0]
        media = b'synthetic original bytes'
        path = archive.audio(note)
        path.parent.mkdir(parents=True)
        path.write_bytes(media)
        store.update_note(1, note['id'], state='ready', audio_bytes=len(media), sha256=hashlib.sha256(media).hexdigest())
        bot = SimpleNamespace(store=store, archive=archive, config=SimpleNamespace(allowed_user_ids={1, 2}))
        runner = web.AppRunner(ExportAPI(bot).application(), access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, '127.0.0.1', 0)
            await site.start()
            print(json.dumps({'origin': f'http://127.0.0.1:{runner.addresses[0][1]}',
                              'code': ObsidianAccess(store).issue(1), 'folders': len(store.folders(1)) - 1}), flush=True)
            stopped = asyncio.Event()
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopped.set)
            await stopped.wait()
        finally:
            await runner.cleanup()
            store.close()


if __name__ == '__main__':
    asyncio.run(main())
