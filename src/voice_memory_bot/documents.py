"""Derived text files and complete Markdown documents; originals stay separate."""
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from .originals import media_label


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = path.with_suffix(path.suffix + ".part")
    with partial.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    partial.replace(path)


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


class TextMaterializer:
    def __init__(self, originals):
        self.originals = originals

    def persist(self, note, folder):
        directory = self.originals.directory(note)
        if note.get("source_json"):
            atomic_write(directory / "source.json", note["source_json"])
        if note["transcript"] is not None:
            atomic_write(directory / "transcript.txt", note["transcript"])
        atomic_write(directory / "note.md", markdown(note, folder))
