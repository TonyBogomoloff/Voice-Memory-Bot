from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from .pdf import history_pdf
from .store import safe_component


def is_audio(note):
    return note["kind"] in {"voice", "audio"} or (note["kind"] == "document" and note.get("origin") != "web")


def media_label(note):
    return "Исходное аудио" if is_audio(note) else {"photo": "Фото", "video": "Видео"}.get(note["kind"], "Файл")


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
        if note.get("origin") == "web":
            extension = note["extension"]
            if not extension.isascii() or not extension.isalnum() or not 1 <= len(extension) <= 12:
                raise ValueError("invalid_upload_extension")
            return self.directory(note) / ("upload." + extension)
        if note["kind"] == "photo" and note["extension"] == "jpg":
            return self.directory(note) / "photo.jpg"
        if note["extension"] not in {"ogg", "mp3", "mp4", "mpeg", "mpga", "m4a", "flac", "wav", "webm", "txt"}:
            raise ValueError("invalid_audio_extension")
        return self.directory(note) / ("voice." + note["extension"])

    @staticmethod
    def markdown(note, folder, audio_name=None, include_audio=True):
        date = datetime.fromtimestamp(note["created_at"], UTC).isoformat()
        meta = f"Проект: {folder['name']}\nДата UTC: {date}\nID: {note['id']}\n"
        photo = note["kind"] == "photo"
        audio_name = audio_name or (f"upload.{note['extension']}" if note.get("origin") == "web"
                                   else "photo.jpg" if photo else f"voice.{note['extension']}")
        label = media_label(note)
        audio = (f"\n[{label}]({quote(audio_name)})\n"
                 if (note["file_id"] or note["audio_bytes"]) and include_audio else "")
        text = note["transcript"] if note["transcript"] is not None else "Расшифровка пока не получена."
        edited = f"\n## Текст заметки\n\n{note['content']}\n" if note.get("content") is not None else ""
        section = "Подпись" if photo else "Исходная расшифровка"
        return f"# {note['title']}\n\n{meta}{audio}{edited}\n## {section}\n\n{text}\n"

    @staticmethod
    def history_markdown(notes, folder, audio_names):
        sections = [f"# {folder['name']}\n\nИстория темы · записей: {len(notes)}\n"]
        for note in notes:
            date = datetime.fromtimestamp(note["created_at"], UTC).isoformat()
            sections.append(f"\n## {note['title']}\n\nДата UTC: {date}\nID: {note['id']}\n")
            if note["id"] in audio_names:
                label = media_label(note)
                sections.append(f"\n[{label}]({quote(audio_names[note['id']])})\n")
            if note.get("entry_type") == "ai":
                sections.append("\n### Ты\n")
            current = note.get("content")
            text = current if current is not None else note["transcript"]
            sections.append("\n" + (text if text is not None else "Расшифровка пока не получена.") + "\n")
            if current is not None and note["transcript"] is not None and current != note["transcript"]:
                sections.append("\n### Исходная расшифровка\n\n" + note["transcript"] + "\n")
            if note.get("ai_response") is not None:
                sections.append("\n### ИИ\n\n" + note["ai_response"] + "\n")
        return "".join(sections)

    def persist_text(self, note, folder):
        directory = self.directory(note)
        if note["transcript"] is not None:
            atomic_write(directory / "transcript.txt", note["transcript"])
        atomic_write(directory / "note.md", self.markdown(note, folder))

    def delete(self, note):
        directory = self.directory(note)
        if directory.exists():
            shutil.rmtree(directory)

    def delete_all(self, owner):
        if type(owner) is not int or owner <= 0:
            raise ValueError("invalid_archive_owner")
        directory = self.root / str(owner)
        if directory.exists():
            shutil.rmtree(directory)

    def export(self, notes, folders, output: Path, include_audio: bool, part_bytes: int,
               *, text_format="pdf", audio_only=False, settings=None):
        """One complete history document per folder, plus optional unmodified audio.

        Split ZIPs at file boundaries, never split or truncate a topic's document.
        Audio checksums describe the bytes actually copied into the archive.
        """
        if text_format not in {"pdf", "markdown"} or audio_only and not include_audio:
            raise ValueError("invalid_export_format")
        notes = sorted(notes, key=lambda n: (n["created_at"], n["id"]))
        folders = list(folders)
        if audio_only:
            notes = [note for note in notes
                     if is_audio(note) and (note["file_id"] or note["audio_bytes"])]
            if not notes:
                return []
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        by_id = {f["id"]: f for f in folders}
        if len({f["owner"] for f in folders} | {n["owner"] for n in notes}) > 1:
            raise ValueError("export_owner_mismatch")
        groups = {f["id"]: [] for f in folders}
        folder_paths = {f["id"]: f"{safe_component(f['name'])}_{f['id']}" for f in folders}
        audio_names, audio_paths = {}, {}
        for note in notes:
            groups[note["folder_id"]].append(note)
            if include_audio and (note["file_id"] or note["audio_bytes"]):
                if not note["audio_bytes"]:
                    raise ValueError("archive_audio_pending")
                original = self.audio(note)
                if not original.is_file() or original.stat().st_size != note["audio_bytes"]:
                    raise ValueError("archive_audio_missing")
                date = datetime.fromtimestamp(note["created_at"], UTC).strftime("%Y-%m-%d_%H-%M-%S")
                media_dir = ("photos" if note["kind"] == "photo" else "audio" if is_audio(note)
                             else "videos" if note["kind"] == "video" else "files")
                audio_names[note["id"]] = f"{media_dir}/{date}_{safe_component(note['title'])}_{note['id']}.{note['extension']}"
                audio_paths[note["id"]] = original
        metadata = {
            "schema_version": 3, "exported_at": datetime.now(UTC).isoformat(),
            "content": "audio" if audio_only else "all" if include_audio else "text",
            "text_format": None if audio_only else text_format,
            "folders": [{"id": f["id"], "name": f["name"], "path": folder_paths[f["id"]],
                         "aliases": json.loads(f["aliases"]), "is_inbox": bool(f["is_inbox"]),
                         "mode": f.get("mode", "notes")} for f in folders],
            "settings": settings or {"text_format": text_format},
        }
        prefix = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))[:-1].encode() + b',"files":['
        suffix = b"]}\n"
        directories = [path + "/" for path in folder_paths.values()]
        part_digits = max(3, len(str(max(1, len(notes) + len(folders)))))
        manifest_name = "manifest-" + "0" * part_digits + ".json"

        def entry_size(name, size):
            # Local header + central directory entry; UTF-8 filenames appear twice.
            return 76 + 2 * len(name.encode("utf-8")) + size

        base_size = (22 + sum(entry_size(name, 0) for name in directories)
                     + entry_size(manifest_name, len(prefix) + len(suffix)))
        if base_size > part_bytes:
            raise ValueError("export_part_too_large")
        paths, handle, used, records = [], None, base_size, []

        def start():
            nonlocal handle, used, records
            path = output / f"voice-memory-{metadata['content']}-{len(paths) + 1:03d}.zip"
            handle = zipfile.ZipFile(path, "x", compression=zipfile.ZIP_STORED)
            paths.append(path)
            used, records = base_size, []
            for name in directories:
                handle.writestr(name, b"")

        def finish():
            nonlocal handle
            if handle:
                handle.writestr(f"manifest-{len(paths):0{part_digits}d}.json", prefix + b",".join(records) + suffix)
                handle.close()
                handle = None

        def items():
            for folder_id, grouped in groups.items():
                if not grouped:
                    continue
                folder_path, folder = folder_paths[folder_id], by_id[folder_id]
                if not audio_only:
                    if text_format == "pdf":
                        name, data = folder_path + "/history.pdf", history_pdf(grouped, folder, audio_names)
                    else:
                        name = folder_path + "/history.md"
                        data = self.history_markdown(grouped, folder, audio_names).encode("utf-8")
                    fields = ("id", "title", "created_at", "kind", "duration", "state", "routing", "transcript",
                              "content", "entry_type", "revision", "revisions", "ai_response", "ai_state",
                              "conversation_id", "ai_model", "original_name", "mime_type", "origin")
                    details = [{k: n[k] for k in fields if k in n} for n in grouped]
                    yield name, data, {"kind": "history", "folder_id": folder_id, "notes": details}
                for note in grouped:
                    if note["id"] in audio_paths:
                        name = folder_path + "/" + audio_names[note["id"]]
                        yield name, audio_paths[note["id"]], {
                            "kind": "audio" if is_audio(note) else note["kind"],
                            "folder_id": folder_id, "note_id": note["id"],
                            "title": note["title"], "created_at": note["created_at"],
                            "duration": note["duration"], "bytes": note["audio_bytes"], "sha256": note["sha256"]}

        try:
            for name, data, record in items():
                record["path"] = name
                if isinstance(data, bytes):
                    record.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
                size = entry_size(name, record["bytes"]) + len(encoded)
                if base_size + size > part_bytes:
                    raise ValueError("export_document_too_large")
                if handle and used + size + bool(records) > part_bytes:
                    finish()
                if handle is None:
                    start()
                if isinstance(data, bytes):
                    handle.writestr(name, data)
                else:
                    digest, copied = hashlib.sha256(), 0
                    with data.open("rb") as original, handle.open(name, "w") as target:
                        while chunk := original.read(1024 * 1024):
                            copied += len(chunk)
                            if copied > record["bytes"]:
                                raise ValueError("archive_audio_checksum_mismatch")
                            digest.update(chunk)
                            target.write(chunk)
                    if copied != record["bytes"] or digest.hexdigest() != record["sha256"]:
                        raise ValueError("archive_audio_checksum_mismatch")
                used += size + bool(records)
                records.append(encoded)
            if not paths:
                start()
            finish()
            if any(path.stat().st_size > part_bytes for path in paths):
                raise ValueError("export_part_too_large")
            return paths
        except BaseException:
            if handle:
                handle.close()
            for path in paths:
                path.unlink(missing_ok=True)
            raise
