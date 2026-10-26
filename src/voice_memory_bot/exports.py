"""Export snapshot, file plan and document generation, before streaming ZIP output."""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .documents import history_markdown
from .obsidian_export import build as build_obsidian
from .originals import is_audio
from .pdf import history_pdf
from .store import safe_component
from .zip_parts import write_parts


@dataclass(frozen=True)
class ExportSnapshot:
    notes: list[dict]
    folders: list[dict]
    settings: dict

    @classmethod
    def capture(cls, notes, folders, settings=None):
        return cls(deepcopy(list(notes)), deepcopy(list(folders)), deepcopy(settings or {}))


@dataclass(frozen=True)
class ExportPlan:
    groups: dict
    folder_paths: dict
    by_id: dict
    audio_names: dict
    audio_paths: dict


def export_items(plan, *, text_format, audio_only, raw_only):
    for folder_id, grouped in plan.groups.items():
        if not grouped:
            continue
        folder_path, folder = plan.folder_paths[folder_id], plan.by_id[folder_id]
        if not audio_only and not raw_only:
            if text_format == "pdf":
                name, data = folder_path + "/history.pdf", history_pdf(grouped, folder, plan.audio_names)
            else:
                name = folder_path + "/history.md"
                data = history_markdown(grouped, folder, plan.audio_names).encode("utf-8")
            fields = ("id", "title", "created_at", "kind", "duration", "state", "routing", "transcript",
                      "content", "entry_type", "revision", "revisions", "ai_response", "ai_state",
                      "conversation_id", "ai_model", "ai_generated_from", "original_name", "mime_type", "origin")
            details = [{k: n[k] for k in fields if k in n} for n in grouped]
            yield name, data, {"kind": "history", "folder_id": folder_id, "notes": details}
        for note in grouped:
            if raw_only:
                source = json.loads(note.get("source_json") or "{}")
                date = datetime.fromtimestamp(note["created_at"], UTC).strftime("%Y-%m-%d_%H-%M-%S")
                base = f"{folder_path}/{date}_{note['id']}"
                details = {"kind": note["kind"], "folder_id": folder_id, "note_id": note["id"],
                           "created_at": note["created_at"], "original_name": note.get("original_name"),
                           "mime_type": note.get("mime_type"), "source": source,
                           "legacy_source": not bool(source)}
                yield base + "/message.json", json.dumps(details, ensure_ascii=False, indent=2).encode(), {
                    "kind": "source_metadata", "folder_id": folder_id, "note_id": note["id"]}
                original_text = source.get("text")
                if original_text is None and note["kind"] == "text":
                    original_text = note["transcript"]
                caption = source.get("caption")
                if caption is None and note["kind"] == "photo":
                    caption = note["transcript"]
                for filename, value in (("text.txt", original_text), ("caption.txt", caption)):
                    if value is not None:
                        yield base + "/" + filename, value.encode("utf-8"), {
                            "kind": "source_text", "folder_id": folder_id, "note_id": note["id"]}
            if note["id"] in plan.audio_paths:
                name = folder_path + "/" + plan.audio_names[note["id"]]
                yield name, plan.audio_paths[note["id"]], {
                    "kind": "audio" if is_audio(note) else note["kind"],
                    "folder_id": folder_id, "note_id": note["id"],
                    "title": note["title"], "created_at": note["created_at"],
                    "duration": note["duration"], "bytes": note["audio_bytes"], "sha256": note["sha256"]}


class ExportBuilder:
    def __init__(self, originals):
        self.originals = originals

    def build(self, notes, folders, output: Path, include_audio: bool, part_bytes: int,
               *, text_format="pdf", audio_only=False, raw_only=False, settings=None, obsidian=False):
        """One complete history document per folder, plus optional unmodified audio.

        Split ZIPs at file boundaries, never split or truncate a topic's document.
        Audio checksums describe the bytes actually copied into the archive.
        """
        snapshot = ExportSnapshot.capture(notes, folders, settings)
        if obsidian:
            return build_obsidian(snapshot, self.originals, output, include_audio, part_bytes)
        notes, folders, settings = snapshot.notes, snapshot.folders, snapshot.settings
        if text_format not in {"pdf", "markdown"} or (audio_only or raw_only) and not include_audio:
            raise ValueError("invalid_export_format")
        notes = sorted(notes, key=lambda n: (n["created_at"], n["id"]))
        if raw_only:
            notes = [note for note in notes if not note.get("ai_generated_from")]
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
                original = self.originals.audio(note)
                if not original.is_file() or original.stat().st_size != note["audio_bytes"]:
                    raise ValueError("archive_audio_missing")
                date = datetime.fromtimestamp(note["created_at"], UTC).strftime("%Y-%m-%d_%H-%M-%S")
                media_dir = ("photos" if note["kind"] == "photo" else "audio" if is_audio(note)
                             else "videos" if note["kind"] in {"video", "video_note"} else "files")
                audio_names[note["id"]] = f"{media_dir}/{date}_{safe_component(note['title'])}_{note['id']}.{note['extension']}"
                if raw_only:
                    # Keep the filename (including extension) under a unique note directory.
                    original_name = (note.get("original_name") or original.name).replace("\\", "/").split("/")[-1]
                    original_name = "".join(c for c in original_name if c.isprintable())[:180]
                    if original_name in {"", ".", ".."}:
                        original_name = "original." + note["extension"]
                    audio_names[note["id"]] = f"{date}_{note['id']}/media/{original_name}"
                audio_paths[note["id"]] = original
        metadata = {
            "schema_version": 3, "exported_at": datetime.now(UTC).isoformat(),
            "content": "raw" if raw_only else "audio" if audio_only else "all" if include_audio else "text",
            "text_format": None if audio_only or raw_only else text_format,
            "folders": [{"id": f["id"], "name": f["name"], "path": folder_paths[f["id"]],
                         "aliases": json.loads(f["aliases"]), "is_inbox": bool(f["is_inbox"]),
                         "mode": f.get("mode", "notes")} for f in folders],
            "settings": settings or {"text_format": text_format},
        }
        plan = ExportPlan(groups, folder_paths, by_id, audio_names, audio_paths)
        directories = [path + "/" for path in folder_paths.values()]
        part_digits = max(3, len(str(max(1, len(notes) * (4 if raw_only else 1) + len(folders)))))
        items = export_items(plan, text_format=text_format, audio_only=audio_only, raw_only=raw_only)
        return write_parts(items, output, metadata, directories, part_digits, part_bytes)
