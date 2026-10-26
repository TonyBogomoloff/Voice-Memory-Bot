"""Warm bundled videos before release; check mode never uploads or sends messages."""
import argparse
import asyncio
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from voice_memory_bot.bot import START_COVER, START_VIDEO
from voice_memory_bot.cleanup import CleanupStore
from voice_memory_bot.config import Config
from voice_memory_bot.database import transaction
from voice_memory_bot.delivery import deletion_error
from voice_memory_bot.help_menu import HELP_COVER, HELP_TOPICS, help_video_path
from voice_memory_bot.providers import ProviderError, Telegram, TelegramError, silent_delivery
from voice_memory_bot.video_cache import CACHE_NAME


def bundled_videos():
    return {**{topic: (help_video_path(topic), 10) for topic in HELP_TOPICS}, 'start': (START_VIDEO, 30), 'help-cover': (HELP_COVER, 0)}


async def prepare(telegram, videos, owner, *, check_only=False, forget_message=None):
    cache = telegram.video_cache
    results = []
    for name, (path, duration) in videos.items():
        digest = cache.digest(path)
        file_id = cache.get(digest)
        if file_id:
            try:
                await telegram.call('getFile', file_id=file_id)
            except TelegramError as exc:
                if str(exc) != 'telegram_400' or exc.reason != 'file_id_invalid':
                    raise
                if not check_only:
                    cache.discard(digest, file_id)
                file_id = None
        if not file_id:
            if check_only:
                raise RuntimeError('video_cache_not_ready:' + name)
            # Track the response before the cache write too, so a write failure can
            # still clean up the exact preparation message without another upload.
            sent = []
            previous = telegram.on_message
            def remember(chat, message_id, thread):
                sent.append((chat, message_id))
                if previous:
                    previous(chat, message_id, thread)
            telegram.on_message = remember
            token = silent_delivery.set(True)
            try:
                options = {'width': 1080, 'height': 1080, 'cover': START_COVER} if path == START_VIDEO else {'width': 1080, 'height': 1080}
                if name == 'help-cover':
                    await telegram.help_media(owner, path, caption='Подготовка помощи')
                else:
                    await telegram.video(owner, path, caption='Подготовка видео', duration=duration, **options)
            finally:
                telegram.on_message = previous
                silent_delivery.reset(token)
                for chat, message_id in sent:
                    try:
                        await telegram.call('deleteMessage', chat_id=chat, message_id=message_id)
                    except ProviderError as exc:
                        if not deletion_error(exc).complete:
                            raise
                    if forget_message:
                        forget_message(chat, message_id)
            if not cache.get(digest):
                raise RuntimeError('video_cache_not_ready:' + name)
            # Avoid flooding the preparation chat; user requests never run this loop.
            await asyncio.sleep(1.1)
        results.append({'name': name, 'sha256': digest, 'uploaded': file_id is None})
    return {'ready': True, 'videos': results, 'uploaded': sum(item['uploaded'] for item in results)}


def receipt_store(path):
    # Preparation uses the installed receipt table, never candidate migrations.
    # Opening Store here would upgrade production before the guarded code swap.
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True)
    db.execute('PRAGMA busy_timeout=5000')
    return SimpleNamespace(db=db, transaction=lambda: transaction(db), close=db.close)


async def main(args):
    # Keep the configured parent: the application may be a mounted-storage symlink.
    app = args.app.absolute()
    shared = app.parent / 'config.json'
    config = Config.load(app / 'config.json', shared)
    owner = int(json.loads(shared.read_text())['my_chat_id'])
    if owner not in config.allowed_user_ids:
        raise RuntimeError('video_cache_preparation_chat_not_authorized')
    telegram = Telegram(config.bot_token, api_base=config.telegram_api_base,
                        file_root=config.telegram_file_root, video_cache=config.data_dir / CACHE_NAME)
    store = None
    try:
        # Do not start a second bot/poller or recover its processing queue.
        if not args.check:
            store = receipt_store(config.data_dir / 'archive.sqlite3')
            telegram.on_message = CleanupStore(store).remember
        def forget(chat, message_id):
            with store.transaction():
                store.db.execute('DELETE FROM telegram_messages WHERE owner=? AND message_id=?', (chat, message_id))
        result = await prepare(telegram, bundled_videos(), owner, check_only=args.check,
                               forget_message=forget if store else None)
        print(json.dumps(result), flush=True)
    finally:
        await telegram.close()
        if store:
            store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--check', action='store_true', help='Verify cached IDs without uploading')
    os.umask(0o077)
    asyncio.run(main(parser.parse_args()))
