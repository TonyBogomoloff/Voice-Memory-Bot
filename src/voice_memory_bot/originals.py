"""Original media locations and deletion, independent of documents and exports."""
import shutil
from pathlib import Path


def is_audio(note):
    return note["kind"] in {"voice", "audio"} or (
        note["kind"] == "document" and note.get("origin") != "web"
        and note["extension"] in {"ogg", "mp3", "mp4", "mpeg", "mpga", "m4a", "flac", "wav", "webm"})


def media_label(note):
    return "Исходное аудио" if is_audio(note) else {"photo": "Фото", "video": "Видео", "video_note": "Видеокружок"}.get(note["kind"], "Файл")


class Originals:
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
        if note.get("origin") == "web" or note["kind"] == "file":
            extension = note["extension"]
            if not extension.isascii() or not extension.isalnum() or not 1 <= len(extension) <= 12:
                raise ValueError("invalid_upload_extension")
            return self.directory(note) / ("upload." + extension)
        if note["kind"] == "photo" and note["extension"] == "jpg":
            return self.directory(note) / "photo.jpg"
        if note["extension"] not in {"ogg", "mp3", "mp4", "mpeg", "mpga", "m4a", "flac", "wav", "webm", "txt"}:
            raise ValueError("invalid_audio_extension")
        return self.directory(note) / ("voice." + note["extension"])

    def delete(self, note):
        directory = self.directory(note)
        if directory.exists():
            shutil.rmtree(directory)
