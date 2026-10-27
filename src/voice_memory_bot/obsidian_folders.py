"""One portable Obsidian document per folder, shared by ZIP and HTTP v2."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import quote

from .obsidian_export import FORMAT, folder_path, identity, media_descriptor
from .originals import is_audio, media_label
from .zip_parts import write_parts

SCHEMA_VERSION = 2
MAX_DOCUMENT = 8 * 1024 * 1024
MAX_ATTACHMENTS = 10000
HISTORY_END = '<!-- voice-memory:history-end -->'


def selected_media(note, audio):
    return note['kind'] == 'photo' or audio and is_audio(note)


def folder_document(notes, folder, *, audio=False, include_media=True):
    folder_id = identity(folder['id'])
    ordered = sorted(notes, key=lambda n: (n['created_at'], n['id']))
    if any(n['owner'] != folder['owner'] or n['folder_id'] != folder_id for n in ordered):
        raise ValueError('export_folder_mismatch')
    if any(n.get('state') in {'queued', 'processing'} for n in ordered):
        raise ValueError('obsidian_folder_pending')
    properties = {'voice_memory_format': SCHEMA_VERSION, 'voice_memory_layout': 'folder',
                  'voice_memory_id': folder_id, 'voice_memory_folder_id': folder_id,
                  'project': folder['name'], 'source': 'Voice Memory', 'export_audio': audio}
    header = '\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False)}' for key, value in properties.items())
    title = ' '.join(folder['name'].splitlines())
    sections = [f'---\n{header}\ntags: [voice-memory]\n---\n\n# {title}\n']
    attachments = []
    for note in ordered:
        note_id = identity(note['id'])
        stamp = datetime.fromtimestamp(note['created_at'], UTC).strftime('%Y-%m-%d %H:%M:%S UTC')
        title = ' '.join(note['title'].splitlines())
        sections.append(f'\n## {stamp} · {title}\n\n<!-- voice-memory:record:{note_id} -->\n\n')
        current, transcript = note.get('content'), note.get('transcript')
        text = current if current is not None else transcript
        sections.append((text if text is not None else 'Текст пока не получен.') + '\n')
        if include_media and selected_media(note, audio):
            media = media_descriptor(note)
            if media:
                attachments.append(media)
                sections.append(f'\n![{media_label(note)}]({quote(media["path"])})\n')
        if current is not None and transcript is not None and current != transcript:
            sections.append('\n### Исходная расшифровка\n\n' + transcript + '\n')
        if note.get('ai_response') is not None:
            sections.append('\n### Ответ ИИ\n\n' + note['ai_response'] + '\n')
    if not ordered:
        sections.append('\nВ папке пока нет записей.\n')
    # Escape marker-shaped user content so the editable suffix has one unambiguous boundary.
    history = ''.join(sections).replace(HISTORY_END, '&lt;!-- voice-memory:history-end --&gt;')
    data = (history + '\n' + HISTORY_END + '\n\n## Мои дополнения\n\n').encode('utf-8')
    if len(data) > MAX_DOCUMENT or len(attachments) > MAX_ATTACHMENTS:
        raise ValueError('export_document_too_large')
    return data, {'kind': 'note', 'layout': 'folder', 'note_id': folder_id, 'folder_id': folder_id,
                  'path': folder_path(folder) + '.md', 'attachments': attachments}


def build(snapshot, originals, output, include_media, part_bytes):
    notes, folders = snapshot.notes, snapshot.folders
    if len({f['owner'] for f in folders} | {n['owner'] for n in notes}) > 1:
        raise ValueError('export_owner_mismatch')
    groups = {f['id']: [] for f in folders}
    for note in notes:
        if note['folder_id'] not in groups:
            raise ValueError('export_folder_mismatch')
        groups[note['folder_id']].append(note)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    choices = snapshot.settings.get('obsidian_audio', {})
    selected = [f for f in folders if not f.get('removed') or groups[f['id']]]
    metadata = {'format': FORMAT, 'schema_version': SCHEMA_VERSION, 'content': 'obsidian', 'layout': 'folder',
                'exported_at': datetime.now(UTC).isoformat(),
                'folders': [{'id': f['id'], 'name': f['name']} for f in selected]}

    def items():
        for folder in selected:
            grouped = groups[folder['id']]
            data, record = folder_document(grouped, folder, audio=bool(choices.get(folder['id'], False)),
                                           include_media=include_media)
            yield record['path'], data, record
            by_id = {n['id']: n for n in grouped}
            for media in record['attachments']:
                path = originals.audio(by_id[media['note_id']])
                if path.is_symlink() or not path.is_file() or path.stat().st_size != media['bytes']:
                    raise ValueError('archive_audio_missing')
                yield media['path'], path, dict(media)

    return write_parts(items(), output, metadata, [], max(3, len(str(len(notes) + len(folders)))), part_bytes)
