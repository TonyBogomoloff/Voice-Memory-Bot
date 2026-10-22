import asyncio
import hashlib
import json
import sqlite3
import zipfile
from io import BytesIO
from urllib.parse import unquote

import pytest
from pypdf import PdfReader

from voice_memory_bot.archive import Archive
from voice_memory_bot.bot import Bot
from voice_memory_bot.config import Config, Limits
from voice_memory_bot.store import Store


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "archive.sqlite3", Limits())
    db.ensure_user(1)
    db.ensure_user(2)
    yield db
    db.close()


def add(store, message=1, owner=1, **values):
    return store.add_note(owner, store.ensure_user(owner), {"message_id": message}, **values)[0]


def saved_audio(store, archive, message=1, transcript="Моя голосовая заметка"):
    note = add(store, message, kind="voice", file_id="PRIVATE_FILE_ID", extension="ogg", transcript=transcript)
    audio = archive.audio(note)
    audio.parent.mkdir(parents=True, exist_ok=True)
    data = b"original audio bytes"
    audio.write_bytes(data)
    store.update_note(1, note["id"], audio_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    return store.note(1, note["id"])


def manifest(zipped):
    return json.loads(zipped.read(next(n for n in zipped.namelist() if n.startswith("manifest-"))))


def test_pdf_export_keeps_full_cyrillic_text_and_empty_folders(tmp_path, store):
    transcript = "Начало беседы. Ёжик, їжак, reunión.\n" + (
        "Длинная мысль о проекте и следующем шаге. " * 30 + "\n") * 15
    transcript += '<img src="https://invalid.example/private"> & <b>буквальный текст</b>\nКонец беседы.'
    note = add(store, kind="text", transcript=transcript)
    followup = add(store, message=2, kind="text", transcript="Следующая реплика в этой же теме.")
    empty = store.create_folder(1, "../../Будущие идеи")
    store.aliases(1, empty["id"], ["Потом"])
    add(store, owner=2, kind="text", transcript="ЧУЖОЙ СЕКРЕТ")
    archive = Archive(tmp_path / "notes")
    notes = store.export_notes(1)
    # The history renderer also retains AI replies, edited content and the original text.
    notes[-1].update(content="Исправленная заметка", entry_type="ai", ai_response="Полный ответ ассистента")
    paths = archive.export(notes, store.folders(1), tmp_path / "export", True, 45_000_000)
    assert len(paths) == 1
    with zipfile.ZipFile(paths[0]) as zipped:
        names = zipped.namelist()
        assert all(not n.startswith("/") and ".." not in n.split("/") for n in names)
        assert len([n for n in names if n.endswith(".pdf")]) == 1
        assert not any(n.endswith((".md", ".txt")) for n in names)
        metadata = manifest(zipped)
        history = metadata["files"][0]
        assert history["notes"][0]["transcript"] == transcript
        assert [n["id"] for n in history["notes"]] == [note["id"], followup["id"]]
        empty_meta = next(f for f in metadata["folders"] if f["id"] == empty["id"])
        assert empty_meta["aliases"] == ["Потом"] and empty_meta["path"] + "/" in names
        pdf = PdfReader(BytesIO(zipped.read(next(n for n in names if n.endswith(".pdf")))))
        assert len(pdf.pages) > 1
        extracted = "\n".join(page.extract_text() for page in pdf.pages)
        assert "Начало беседы. Ёжик, їжак, reunión." in extracted
        assert "Конец беседы." in extracted and "<b>буквальный текст</b>" in extracted
        assert extracted.index("Начало беседы") < extracted.index("Исправленная заметка")
        assert "Следующая реплика в этой же теме." in extracted and "Полный ответ ассистента" in extracted
        assert "ЧУЖОЙ СЕКРЕТ" not in extracted
        assert not pdf.attachments
        assert not any(page.get("/Annots") for page in pdf.pages)


def test_markdown_links_to_original_and_audio_only_contains_no_transcripts(tmp_path, store):
    archive = Archive(tmp_path / "notes")
    note = saved_audio(store, archive, transcript="  Исходный текст\n\nс переносами.  ")
    add(store, message=2, kind="text", transcript="ONLY_TEXT_NOTE")
    notes, folders = store.export_notes(1), store.folders(1)
    paths = archive.export(notes, folders, tmp_path / "md", True, 45_000_000, text_format="markdown")
    with zipfile.ZipFile(paths[0]) as zipped:
        metadata = manifest(zipped)
        history = next(f for f in metadata["files"] if f["kind"] == "history")
        row = next(n for n in history["notes"] if n["id"] == note["id"])
        assert row["transcript"] == note["transcript"]
        md_path = history["path"]
        markdown = zipped.read(md_path).decode()
        audio_link = markdown.split("[Исходное аудио](", 1)[1].split(")", 1)[0]
        linked_path = md_path.rsplit("/", 1)[0] + "/" + unquote(audio_link)
        assert zipped.read(linked_path) == b"original audio bytes"
        audio = next(f for f in metadata["files"] if f["kind"] == "audio")
        assert hashlib.sha256(zipped.read(linked_path)).hexdigest() == audio["sha256"]
        assert "ONLY_TEXT_NOTE" in markdown and len([n for n in zipped.namelist() if n.endswith(".md")]) == 1
        assert not any(n.endswith(".pdf") for n in zipped.namelist())
    paths = archive.export(notes, folders, tmp_path / "audio", True, 45_000_000, audio_only=True)
    with zipfile.ZipFile(paths[0]) as zipped:
        metadata = manifest(zipped)
        assert len(metadata["files"]) == 1
        assert "notes" not in metadata["files"][0]
        assert not any(n.endswith((".pdf", ".md", ".txt")) for n in zipped.namelist())
        assert not any(b"ONLY_TEXT_NOTE" in zipped.read(n) for n in zipped.namelist())
        assert metadata["files"][0]["note_id"] == note["id"]


@pytest.mark.parametrize("failure", ["pending", "missing"])
def test_incomplete_audio_aborts_all_parts_but_text_remains_exportable(tmp_path, store, failure):
    archive = Archive(tmp_path / "notes")
    saved_audio(store, archive)
    note = saved_audio(store, archive, message=2)
    if failure == "pending":
        store.update_note(1, note["id"], audio_bytes=0, sha256=None)
    else:
        archive.audio(note).unlink()
    output = tmp_path / "broken"
    with pytest.raises(ValueError, match="archive_audio_"):
        archive.export(store.export_notes(1), store.folders(1), output, True, 45_000_000)
    assert list(output.glob("*.zip")) == []
    paths = archive.export(store.export_notes(1), store.folders(1), tmp_path / "text", False,
                           45_000_000, text_format="markdown")
    with zipfile.ZipFile(paths[0]) as zipped:
        assert len(manifest(zipped)["files"][0]["notes"]) == 2
        assert all("[Исходное аудио]" not in zipped.read(n).decode()
                   for n in zipped.namelist() if n.endswith(".md"))


def test_settings_migrate_existing_database_and_survive_restart(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, active_folder TEXT, action TEXT)")
        old.execute("INSERT INTO users(id) VALUES (1)")
    db = Store(path, Limits())
    assert db.export_format(1) == "pdf"
    db.set_export_format(1, "markdown")
    with pytest.raises(ValueError):
        db.set_export_format(1, "html")
    assert db.export_format(2) == "pdf"
    db.close()
    db = Store(path, Limits())
    assert db.export_format(1) == "markdown"
    assert db.export_format(2) == "pdf"
    db.close()


def test_large_audio_splits_zip_without_splitting_topic_document(tmp_path, store):
    archive = Archive(tmp_path / "notes")
    for message in (1, 2):
        note = saved_audio(store, archive, message=message)
        data = bytes([message]) * 4000
        archive.audio(note).write_bytes(data)
        store.update_note(1, note["id"], audio_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    paths = archive.export(store.export_notes(1), store.folders(1), tmp_path / "parts", True,
                           6500, text_format="markdown")
    assert len(paths) > 1 and all(p.stat().st_size <= 6500 for p in paths)
    files, records, manifests = {}, [], []
    for path in paths:
        with zipfile.ZipFile(path) as zipped:
            files.update({n: zipped.read(n) for n in zipped.namelist() if not n.endswith("/")})
            records.extend(manifest(zipped)["files"])
            manifests.extend(n for n in zipped.namelist() if n.startswith("manifest-"))
    assert len(set(manifests)) == len(paths)
    history = [r for r in records if r["kind"] == "history"]
    audio = [r for r in records if r["kind"] == "audio"]
    assert len(history) == 1 and len(history[0]["notes"]) == 2
    assert len(audio) == 2
    assert {files[r["path"]] for r in audio} == {bytes([1]) * 4000, bytes([2]) * 4000}
    markdown = files[history[0]["path"]].decode()
    for record in audio:
        assert record["path"].split("/", 1)[1] == unquote(markdown.split("[Исходное аудио](", 1)[1].split(")", 1)[0])
        markdown = markdown.split(")", 1)[1]


def test_empty_export_preserves_folders_and_preferences(tmp_path, store):
    empty = store.create_folder(1, "Планы")
    store.set_export_format(1, "markdown")
    archive = Archive(tmp_path / "notes")
    paths = archive.export([], store.folders(1), tmp_path / "empty", True, 45_000_000,
                           text_format="markdown", settings=store.export_preferences(1))
    with zipfile.ZipFile(paths[0]) as zipped:
        metadata = manifest(zipped)
        assert metadata["files"] == [] and metadata["settings"]["text_format"] == "markdown"
        folder = next(f for f in metadata["folders"] if f["id"] == empty["id"])
        assert folder["path"] + "/" in zipped.namelist()


class ExportTelegram:
    def __init__(self):
        self.sent, self.documents = [], []

    async def call(self, method, **values):
        self.sent.append((method, values))
        return {"message_id": len(self.sent)}

    async def text(self, owner, text, thread=None, keys=None):
        return await self.call("sendMessage", chat_id=owner, text=text, reply_markup=keys)

    async def document(self, owner, path, thread=None, caption=None):
        self.documents.append((owner, path.read_bytes(), caption))


def test_export_buttons_settings_scope_and_lowered_quota(tmp_path, store):
    async def run():
        telegram = ExportTelegram()
        config = Config("123:secret", frozenset({1, 2}), "secret", tmp_path,
                        limits=Limits(max_notes_per_user=1))
        bot = Bot(config, store, telegram, None, None)
        saved_audio(store, bot.archive)
        add(store, message=2, kind="text", transcript="Second note despite lowered quota")
        add(store, owner=2, kind="text", transcript="OTHER USER")
        await bot.command(1, None, "/export", "")
        menu = telegram.sent[-1][1]["reply_markup"]["inline_keyboard"]
        assert {b["callback_data"] for row in menu for b in row} >= {
            "zip:all:all", "zip:all:text", "zip:all:voice", "exportsettings:all"}
        await bot.callback(1, None, 50, "settings:all")
        await bot.callback(1, None, 50, "exportformat:markdown:all")
        assert telegram.sent[-1][0] == "editMessageText"
        assert store.export_format(1) == "markdown" and store.export_format(2) == "pdf"
        await bot.callback(1, None, 50, "zip:all:text")
        await asyncio.gather(*bot.export_tasks)
        with zipfile.ZipFile(BytesIO(telegram.documents[-1][1])) as zipped:
            assert len(manifest(zipped)["files"][0]["notes"]) == 2
            assert len([n for n in zipped.namelist() if n.endswith(".md")]) == 1
            assert not any(n.endswith(".ogg") for n in zipped.namelist())
        await bot.callback(1, None, 50, "zip:all:voice")
        await asyncio.gather(*bot.export_tasks)
        with zipfile.ZipFile(BytesIO(telegram.documents[-1][1])) as zipped:
            assert len(manifest(zipped)["files"]) == 1
            assert len([n for n in zipped.namelist() if n.endswith(".ogg")]) == 1
        await bot.callback(1, None, 50, "exportformat:pdf:all")
        # Legacy buttons mean all data, not the new audio-only export.
        await bot.callback(1, None, 50, "zip:all:audio")
        await asyncio.gather(*bot.export_tasks)
        with zipfile.ZipFile(BytesIO(telegram.documents[-1][1])) as zipped:
            assert len([n for n in zipped.namelist() if n.endswith(".pdf")]) == 1
            assert len([n for n in zipped.namelist() if n.endswith(".ogg")]) == 1
        private_folder = store.ensure_user(2)
        with pytest.raises(ValueError):
            await bot.callback(1, None, 50, f"zip:{private_folder}:text")
        assert not list((tmp_path / "exports").iterdir())
    asyncio.run(run())
