"""Loopback HTTP API for the one-way Obsidian connector. No note-write routes."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from pathlib import Path

from aiohttp import web

from .obsidian_access import AccessError, ObsidianAccess
from .obsidian_export import FORMAT, IDENTITY, SCHEMA_VERSION, document
from .obsidian_folders import SCHEMA_VERSION as FOLDER_SCHEMA_VERSION
from .obsidian_folders import folder_document, selected_media
from .obsidian_settings import ObsidianSettings

log = logging.getLogger(__name__)
DEVICE = web.RequestKey("obsidian_device", dict)


class RateLimits:
    def __init__(self):
        self.windows = OrderedDict()

    def allow(self, key, limit):
        now = time.monotonic()
        start, count = self.windows.get(key, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        self.windows[key] = (start, count + 1)
        self.windows.move_to_end(key)
        while len(self.windows) > 2048:
            self.windows.popitem(last=False)
        return count < limit


def checksum(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


class ExportAPI:
    def __init__(self, bot):
        self.bot, self.store = bot, bot.store
        self.access = ObsidianAccess(bot.store)
        self.limits = RateLimits()
        self.media_slots = asyncio.Semaphore(4)

    @web.middleware
    async def guard(self, request, handler):
        try:
            if request.path.startswith(('/v1/', '/v2/')) and request.path != '/v1/pair':
                authorization = request.headers.get('Authorization', '')
                if not authorization.startswith('Bearer '):
                    raise AccessError('unauthorized')
                device = self.access.authenticate(authorization[7:], self.bot.config.allowed_user_ids)
                request[DEVICE] = device
                if not self.limits.allow('device:' + device['id'], 600):
                    raise web.HTTPTooManyRequests(headers={'Retry-After': '60'})
            elif request.path == '/v1/pair':
                # This service binds loopback. Cloudflare overwrites this header at the edge.
                peer = request.headers.get('CF-Connecting-IP', request.remote or '')
                if (not self.limits.allow('pair:global', 60)
                        or not self.limits.allow('pair:' + peer[:64], 10)):
                    raise web.HTTPTooManyRequests(headers={'Retry-After': '60'})
            response = await handler(request)
        except AccessError:
            response = web.json_response({'error': 'unauthorized'}, status=401)
        except web.HTTPException as exc:
            response = web.json_response({'error': exc.reason}, status=exc.status,
                                         headers={'Retry-After': exc.headers['Retry-After']} if 'Retry-After' in exc.headers else None)
        except (ValueError, KeyError, TypeError):
            response = web.json_response({'error': 'invalid_request'}, status=400)
        except Exception as exc:
            log.error('obsidian request failed type=%s', type(exc).__name__)
            response = web.json_response({'error': 'unavailable'}, status=503)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    async def health(self, request):
        return web.json_response({'service': 'voice-memory-obsidian', 'format': FORMAT,
                                  'schema_version': SCHEMA_VERSION, 'folder_schema_version': FOLDER_SCHEMA_VERSION})

    async def pair(self, request):
        payload = await request.json()
        result = self.access.pair(payload.get('code'), payload.get('name'), self.bot.config.allowed_user_ids)
        return web.json_response({'schema_version': SCHEMA_VERSION, **result})

    def render(self, note):
        if note['state'] in {'queued', 'processing'}:
            raise web.HTTPConflict(reason='note_pending')
        folder = self.store.one('SELECT * FROM folders WHERE id=? AND owner=?', (note['folder_id'], note['owner']))
        if not folder:
            raise web.HTTPNotFound()
        try:
            audio = ObsidianSettings(self.store).audio(note['owner'], note['folder_id'])
            data, row = document(note, folder, include_media=selected_media(note, audio))
        except ValueError:
            raise web.HTTPConflict(reason='media_pending') from None
        row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        return data, row

    def query(self, device, *, after='', note_id=None, limit=51):
        sql = ('SELECT n.*, CASE WHEN a.result_note_id IS NULL THEN a.response END AS ai_response '
               'FROM notes n LEFT JOIN ai_requests a ON a.note_id=n.id '
               'WHERE n.owner=? AND n.deleted=0')
        args = [device['owner']]
        if device['folder_id']:
            sql += ' AND n.folder_id=?'
            args.append(device['folder_id'])
        if note_id:
            sql += ' AND n.id=?'
            args.append(note_id)
        else:
            sql += ' AND n.id>?'
            args.append(after)
        return self.store.all(sql + ' ORDER BY n.id LIMIT ?', [*args, limit])

    async def notes(self, request):
        cursor = request.query.get('cursor', '')
        if cursor and not IDENTITY.fullmatch(cursor):
            raise ValueError('invalid_cursor')
        notes = self.query(request[DEVICE], after=cursor)
        rows, pending = [], 0
        for note in notes[:50]:
            try:
                _, row = self.render(note)
                rows.append(row)
            except web.HTTPConflict:
                pending += 1
        return web.json_response({'format': FORMAT, 'schema_version': SCHEMA_VERSION,
                                  'notes': rows, 'pending': pending,
                                  'next_cursor': notes[49]['id'] if len(notes) > 50 else None})

    def selected(self, request):
        note_id = request.match_info['note_id']
        if not IDENTITY.fullmatch(note_id):
            raise web.HTTPNotFound()
        rows = self.query(request[DEVICE], note_id=note_id)
        if not rows:
            raise web.HTTPNotFound()
        return rows[0]

    async def markdown(self, request):
        data, row = self.render(self.selected(request))
        if request.query.get('sha256') != row['sha256']:
            raise web.HTTPConflict(reason='note_changed')
        return web.Response(body=data, content_type='text/markdown', charset='utf-8')

    async def media(self, request):
        note = self.selected(request)
        _, row = self.render(note)
        if not row['attachments']:
            raise web.HTTPNotFound()
        media = row['attachments'][0]
        if request.query.get('sha256') != media['sha256']:
            raise web.HTTPConflict(reason='media_changed')
        path = self.bot.archive.audio(note)
        root = self.bot.archive.root.resolve()
        if (path.is_symlink() or not path.resolve().is_relative_to(root)
                or not path.is_file() or path.stat().st_size != media['bytes']):
            raise web.HTTPConflict(reason='media_unavailable')
        async with self.media_slots:
            if await asyncio.to_thread(checksum, path) != media['sha256']:
                raise web.HTTPConflict(reason='media_checksum')
            # Stream a verified immutable original; never buffer large media in RAM.
            response = web.FileResponse(path, headers={'Content-Type': 'application/octet-stream',
                                                       'Content-Disposition': 'attachment', 'Cache-Control': 'no-store'})
        return response

    def folder_query(self, device, *, after='', folder_id=None):
        sql = ('SELECT f.* FROM folders f WHERE f.owner=? AND (f.removed=0 OR EXISTS '
               '(SELECT 1 FROM notes n WHERE n.folder_id=f.id AND n.owner=f.owner AND n.deleted=0))')
        args = [device['owner']]
        if device['folder_id']:
            sql += ' AND f.id=?'
            args.append(device['folder_id'])
        if folder_id:
            sql += ' AND f.id=?'
            args.append(folder_id)
        else:
            sql += ' AND f.id>?'
            args.append(after)
        return self.store.all(sql + ' ORDER BY f.id LIMIT 21', args)

    def render_folder(self, folder):
        with self.store.transaction():
            notes = self.store.export_notes(folder['owner'], folder['id'])
            audio = ObsidianSettings(self.store).audio(folder['owner'], folder['id'])
            try:
                data, row = folder_document(notes, folder, audio=audio)
            except ValueError as exc:
                if str(exc) in {'obsidian_folder_pending', 'archive_audio_pending', 'invalid_archive_media'}:
                    raise web.HTTPConflict(reason='folder_pending') from None
                raise
        row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        return data, row

    async def folders(self, request):
        cursor = request.query.get('cursor', '')
        if cursor and not IDENTITY.fullmatch(cursor):
            raise ValueError('invalid_cursor')
        folders = self.folder_query(request[DEVICE], after=cursor)
        rows, pending = [], 0
        for folder in folders[:20]:
            try:
                _, row = self.render_folder(folder)
                rows.append(row)
            except web.HTTPConflict:
                pending += 1
        return web.json_response({'format': FORMAT, 'schema_version': FOLDER_SCHEMA_VERSION,
                                  'notes': rows, 'pending': pending,
                                  'next_cursor': folders[19]['id'] if len(folders) > 20 else None})

    async def folder_markdown(self, request):
        folder_id = request.match_info['folder_id']
        if not IDENTITY.fullmatch(folder_id):
            raise web.HTTPNotFound()
        folders = self.folder_query(request[DEVICE], folder_id=folder_id)
        if not folders:
            raise web.HTTPNotFound()
        data, row = self.render_folder(folders[0])
        if request.query.get('sha256') != row['sha256']:
            raise web.HTTPConflict(reason='folder_changed')
        return web.Response(body=data, content_type='text/markdown', charset='utf-8')

    async def plugin(self, request):
        name = request.match_info['name']
        if name not in {'main.js', 'manifest.json', 'styles.css', 'voice-memory.zip'}:
            raise web.HTTPNotFound()
        path = Path(__file__).resolve().parents[2] / 'plugins/obsidian/dist' / name
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    def application(self):
        app = web.Application(middlewares=[self.guard], client_max_size=4096)
        app.add_routes([web.get('/healthz', self.health), web.post('/v1/pair', self.pair),
                        web.get('/v1/notes', self.notes),
                        web.get('/v1/notes/{note_id}/document', self.markdown),
                        web.get('/v1/notes/{note_id}/media', self.media),
                        web.get('/v2/folders', self.folders),
                        web.get('/v2/folders/{folder_id}/document', self.folder_markdown),
                        web.get('/v2/media/{note_id}', self.media),
                        web.get('/plugin/{name}', self.plugin)])
        return app


async def start_api(bot):
    if not bot.config.obsidian_url:
        return None
    runner = web.AppRunner(ExportAPI(bot).application(), access_log=None,
                           keepalive_timeout=15, shutdown_timeout=10)
    await runner.setup()
    try:
        await web.TCPSite(runner, '127.0.0.1', bot.config.obsidian_port).start()
    except BaseException:
        await runner.cleanup()
        raise
    log.info('obsidian export API ready port=%s', bot.config.obsidian_port)
    return runner
