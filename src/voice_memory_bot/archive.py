"""Compatibility facade for originals, derived documents and complete exports."""
from pathlib import Path

from .documents import TextMaterializer, history_markdown, markdown
from .documents import atomic_write as atomic_write
from .exports import ExportBuilder
from .originals import Originals
from .originals import is_audio as is_audio
from .originals import media_label as media_label


class Archive:
    markdown = staticmethod(markdown)
    history_markdown = staticmethod(history_markdown)

    def __init__(self, root: Path):
        self.originals = Originals(root)
        self.root = self.originals.root
        self.documents = TextMaterializer(self.originals)
        self.exporter = ExportBuilder(self.originals)

    def directory(self, note):
        return self.originals.directory(note)

    def audio(self, note):
        return self.originals.audio(note)

    def persist_text(self, note, folder):
        return self.documents.persist(note, folder)

    def delete(self, note):
        return self.originals.delete(note)

    def export(self, notes, folders, output, include_audio, part_bytes, **options):
        return self.exporter.build(notes, folders, output, include_audio, part_bytes, **options)
