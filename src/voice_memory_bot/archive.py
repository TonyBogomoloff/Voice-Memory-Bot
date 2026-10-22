from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from .store import safe_component


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = path.with_suffix(path.suffix + ".part")
    with partial.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    partial.replace(path)


class Archive:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def directory(self, note):
        # IDs come only from Store; project names and LLM output never become filesystem paths.
        owner, identity = note["owner"], note["id"]
        if type(owner) is not int or not identity.isalnum() or len(identity) != 16:
            raise ValueError("invalid_archive_identity")
        return self.root / str(owner) / identity

    def audio(self, note):
        if note["extension"] not in {"ogg", "mp3", "mp4", "mpeg", "mpga", "m4a", "flac", "wav", "webm", "txt"}:
            raise ValueError("invalid_audio_extension")
        return self.directory(note) / ("voice." + note["extension"])

    @staticmethod
    def markdown(note, folder):
        date = datetime.fromtimestamp(note["created_at"], UTC).isoformat()
        meta = f"Проект: {folder['name']}\nДата UTC: {date}\nID: {note['id']}\n"
        audio = f"\n[Исходное аудио](voice.{note['extension']})\n" if note["file_id"] else ""
        text = note["transcript"] if note["transcript"] is not None else "Расшифровка пока не получена."
        return f"# {note['title']}\n\n{meta}{audio}\n## Расшифровка\n\n{text}\n"

    def persist_text(self, note, folder):
        directory = self.directory(note)
        if note["transcript"] is not None:
            atomic_write(directory / "transcript.txt", note["transcript"])
        atomic_write(directory / "note.md", self.markdown(note, folder))

    def delete(self, note):
        directory = self.directory(note)
        if directory.exists():
            shutil.rmtree(directory)

    def export(self, notes, folders, output: Path, include_audio: bool, part_bytes: int):
        """Split at note boundaries. Never silently omit audio that should have been archived."""
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        by_id = {f["id"]: f for f in folders}
        paths, handle, used = [], None, 0
        manifest = []

        def finish():
            nonlocal handle
            if handle:
                handle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                handle.close()
                handle = None

        try:
            for note in notes:
                folder = by_id[note["folder_id"]]
                date = datetime.fromtimestamp(note["created_at"], UTC).strftime("%Y-%m-%d_%H-%M-%S")
                prefix = f"{safe_component(folder['name'])}_{folder['id']}/{date}_{note['id']}"
                markdown = self.markdown(note, folder)
                if not include_audio:
                    markdown = markdown.replace(f"\n[Исходное аудио](voice.{note['extension']})\n", "\n")
                text_files = {"note.md": markdown}
                if note["transcript"] is not None:
                    text_files["transcript.txt"] = note["transcript"]
                audio = self.audio(note) if include_audio and note["audio_bytes"] else None
                if audio and (not audio.exists() or audio.stat().st_size != note["audio_bytes"]):
                    raise ValueError("archive_audio_missing")
                size = sum(len(s.encode()) for s in text_files.values()) + (audio.stat().st_size if audio else 0)
                # ZIP_STORED is predictable for already-compressed audio. Reserve space for metadata.
                size += 8192
                if size > part_bytes:
                    raise ValueError("export_note_too_large")
                if handle and used + size > part_bytes:
                    finish()
                if handle is None:
                    path = output / f"voice-memory-{len(paths) + 1:03d}.zip"
                    paths.append(path)
                    handle, used, manifest = zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED), 0, []
                for name, text in text_files.items():
                    handle.writestr(f"{prefix}/{name}", text)
                if audio:
                    with audio.open("rb") as original:
                        checksum = hashlib.file_digest(original, "sha256").hexdigest()
                    if checksum != note["sha256"]:
                        raise ValueError("archive_audio_checksum_mismatch")
                    handle.write(audio, f"{prefix}/{audio.name}")
                manifest.append({"id": note["id"], "project": folder["name"], "path": prefix,
                                 "state": note["state"], "audio_included": bool(audio),
                                 "audio_sha256": note["sha256"] if audio else None})
                used += size
            finish()
            if any(path.stat().st_size > part_bytes for path in paths):
                raise ValueError("export_part_too_large")
            return paths
        finally:
            if handle:
                handle.close()
