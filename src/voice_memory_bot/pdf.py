from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from threading import Lock
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

_render_lock = Lock()
_font = "VoiceMemory"
_bold = "VoiceMemoryBold"


def history_pdf(notes: list[dict], folder: dict, audio_names: dict[str, str]) -> bytes:
    """Render plain user text; never interpret it as ReportLab markup or fetch resources."""
    # ReportLab's font registry is global; serialize PDF work across export threads.
    with _render_lock:
        if _font not in pdfmetrics.getRegisteredFontNames():
            fonts = Path(__file__).with_name("fonts")
            pdfmetrics.registerFont(TTFont(_font, str(fonts / "DejaVuSans.ttf")))
            pdfmetrics.registerFont(TTFont(_bold, str(fonts / "DejaVuSans-Bold.ttf")))
        buffer = BytesIO()
        document = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=48, leftMargin=48,
                                     topMargin=48, bottomMargin=48, title=folder["name"],
                                     author="Voice Memory Bot", pageCompression=1)
        body = ParagraphStyle("Body", fontName=_font, fontSize=11, leading=17,
                              textColor=HexColor("#e2e8eb"), spaceAfter=5, splitLongWords=True)
        title = ParagraphStyle("Title", parent=body, fontName=_bold, fontSize=20, leading=27,
                               textColor=HexColor("#72dfcf"), spaceAfter=16)
        meta = ParagraphStyle("Meta", parent=body, fontSize=9, leading=14,
                              textColor=HexColor("#b7c5cc"))
        heading = ParagraphStyle("Heading", parent=title, fontSize=13, leading=19,
                                 spaceBefore=12, spaceAfter=6, keepWithNext=True)
        role = ParagraphStyle("Role", parent=body, fontName=_bold, spaceBefore=8, keepWithNext=True)

        def page(canvas, doc):
            canvas.saveState()
            canvas.setFillColor(HexColor("#22292e"))
            canvas.rect(0, 0, A4[0], A4[1], stroke=0, fill=1)
            canvas.setFont(_font, 8)
            canvas.setFillColor(HexColor("#b7c5cc"))
            canvas.drawString(48, 26, "Voice Memory Bot")
            canvas.drawRightString(A4[0] - 48, 26, f"{doc.page}")
            canvas.restoreState()

        flow = [Paragraph(escape(folder["name"]), title),
                Paragraph(f"История темы · записей: {len(notes)}", meta), Spacer(1, 14)]

        def add_text(text):
            for line in text.split("\n"):
                flow.append(Paragraph(escape(line), body) if line.strip() else Spacer(1, 8))

        for note in notes:
            date = datetime.fromtimestamp(note["created_at"], UTC).strftime("%d.%m.%Y %H:%M UTC")
            flow.extend([Paragraph(escape(note["title"]), heading),
                         Paragraph(date + " · ID: " + escape(note["id"]), meta)])
            if note["id"] in audio_names:
                label = "Фото: " if note["kind"] == "photo" else "Аудио: "
                flow.append(Paragraph(label + escape(audio_names[note["id"]]), meta))
            if note.get("entry_type") == "ai":
                flow.append(Paragraph("Ты", role))
            current = note.get("content")
            text = current if current is not None else note["transcript"]
            add_text(text if text is not None else "Расшифровка пока не получена.")
            if current is not None and note["transcript"] is not None and current != note["transcript"]:
                flow.append(Paragraph("Исходная расшифровка", role))
                add_text(note["transcript"])
            if note.get("ai_response") is not None:
                flow.append(Paragraph("ИИ", role))
                add_text(note["ai_response"])
            flow.append(Spacer(1, 12))
        document.build(flow, onFirstPage=page, onLaterPages=page)
        return buffer.getvalue()
