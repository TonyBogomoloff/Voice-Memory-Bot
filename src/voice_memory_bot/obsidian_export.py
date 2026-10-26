"""Portable, owner-scoped Obsidian documents shared by ZIP and read-only API."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import quote

from .originals import is_audio, media_label
from .store import safe_component
from .zip_parts import write_parts

FORMAT = "voice-memory-obsidian"
SCHEMA_VERSION = 1
IDENTITY = re.compile(r"[a-zA-Z0-9]{16}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def identity(value):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise ValueError("invalid_archive_identity")
    return value


def folder_path(folder):
    return f"{safe_component(folder['name'])}_{identity(folder['id'])}"


def media_descriptor(note):
    if not (note["file_id"] or note["audio_bytes"]):
        return None
    if not note["audio_bytes"]:
        raise ValueError("archive_audio_pending")
    digest, extension = note["sha256"], note["extension"]
    if (not isinstance(digest, str) or not DIGEST.fullmatch(digest)
            or not re.fullmatch(r"[a-zA-Z0-9]{1,12}", extension)):
        raise ValueError("invalid_archive_media")
    return {"kind": "attachment", "note_id": identity(note["id"]),
            "path": f"attachments/{note['id']}/{digest}.{extension}",
            "bytes": note["audio_bytes"], "sha256": digest}


def document(note, folder, *, include_media=True):
    """Return UTF-8 Markdown and only portable public metadata."""
    note_id = identity(note["id"])
    path = f"{folder_path(folder)}/{safe_component(note['title'])}_{note_id}.md"
    media = media_descriptor(note) if include_media else None
    properties = {
        "voice_memory_format": SCHEMA_VERSION,
        "voice_memory_id": note_id,
        "voice_memory_revision": note.get("revision", 0),
        "voice_memory_folder_id": identity(folder["id"]),
        "created": datetime.fromtimestamp(note["created_at"], UTC).isoformat(),
        "project": folder["name"],
        "source": "Voice Memory",
        "type": note["kind"],
    }
    frontmatter = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}"
                            for key, value in properties.items())
    title = " ".join(note["title"].splitlines())
    current = note.get("content")
    transcript = note.get("transcript")
    text = current if current is not None else transcript
    sections = [f"---\n{frontmatter}\ntags: [voice-memory]\n---\n\n# {title}\n\n",
                text if text is not None else "Текст пока не получен.", "\n"]
    if media:
        label = media_label(note)
        embed = "!" if is_audio(note) or note["kind"] in {"photo", "video", "video_note"} else ""
        sections.append(f"\n{embed}[{label}]({quote('../' + media['path'])})\n")
    if current is not None and transcript is not None and current != transcript:
        sections.append("\n## Исходная расшифровка\n\n" + transcript + "\n")
    if note.get("ai_response") is not None:
        sections.append("\n## Ответ ИИ\n\n" + note["ai_response"] + "\n")
    data = "".join(sections).encode("utf-8")
    return data, {"kind": "note", "note_id": note_id, "folder_id": folder["id"],
                  "path": path, "attachments": [media] if media else []}


def build(snapshot, originals, output, include_media, part_bytes):
    notes, folders = snapshot.notes, snapshot.folders
    if len({f["owner"] for f in folders} | {n["owner"] for n in notes}) > 1:
        raise ValueError("export_owner_mismatch")
    by_id = {f["id"]: f for f in folders}
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = {"format": FORMAT, "schema_version": SCHEMA_VERSION, "content": "obsidian",
                "exported_at": datetime.now(UTC).isoformat(),
                "folders": [{"id": f["id"], "name": f["name"], "path": folder_path(f)} for f in folders]}

    def items():
        for note in sorted(notes, key=lambda n: (n["created_at"], n["id"])):
            folder = by_id.get(note["folder_id"])
            if folder is None:
                raise ValueError("export_folder_mismatch")
            data, record = document(note, folder, include_media=include_media)
            yield record["path"], data, record
            for media in record["attachments"]:
                original = originals.audio(note)
                if original.is_symlink() or not original.is_file() or original.stat().st_size != media["bytes"]:
                    raise ValueError("archive_audio_missing")
                yield media["path"], original, dict(media)

    directories = [folder_path(f) + "/" for f in folders]
    return write_parts(items(), output, metadata, directories,
                       max(3, len(str(max(1, len(notes) * 2)))), part_bytes)
