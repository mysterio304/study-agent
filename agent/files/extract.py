"""Text extraction with source locators (SPEC §7): PDF per page, PPTX per slide (with speaker
notes), DOCX per section. Pure functions over a local path — no network, no LLM.

Scanned PDFs (no text layer) produce no segments; OCR is out of scope.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SUPPORTED = {".pdf": "pdf", ".pptx": "pptx", ".docx": "docx"}


@dataclass(frozen=True, slots=True)
class Segment:
    locator: str  # "p. 12" | "slide 7" | "§3"
    text: str


class ExtractionError(RuntimeError):
    pass


def kind_of(name: str) -> str | None:
    return SUPPORTED.get(Path(name).suffix.lower())


def _clean(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def extract(path: Path, name: str | None = None) -> list[Segment]:
    kind = kind_of(name or path.name)
    if kind is None:
        raise ExtractionError(f"Unsupported file type: {path.suffix}")
    try:
        if kind == "pdf":
            return _pdf(path)
        if kind == "pptx":
            return _pptx(path)
        return _docx(path)
    except ExtractionError:
        raise
    except Exception as exc:  # corrupt/encrypted files must not crash the indexer
        raise ExtractionError(f"Could not read {path.name}: {type(exc).__name__}") from exc


def _pdf(path: Path) -> list[Segment]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ExtractionError("Encrypted PDF") from exc
    out = []
    for i, page in enumerate(reader.pages, 1):
        text = _clean(page.extract_text() or "")
        if text:
            out.append(Segment(f"p. {i}", text))
    return out


def _pptx(path: Path) -> list[Segment]:
    from pptx import Presentation

    out = []
    for i, slide in enumerate(Presentation(str(path)).slides, 1):
        parts = [
            shape.text_frame.text
            for shape in slide.shapes
            if getattr(shape, "has_text_frame", False)
        ]
        for shape in slide.shapes:
            if getattr(shape, "has_table", False):
                parts += [" | ".join(c.text for c in row.cells) for row in shape.table.rows]
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text
            if notes.strip():
                parts.append(f"Notes: {notes}")
        text = _clean("\n".join(parts))
        if text:
            out.append(Segment(f"slide {i}", text))
    return out


def _docx(path: Path) -> list[Segment]:
    from docx import Document

    out: list[Segment] = []
    section, buf = 1, []
    for para in Document(str(path)).paragraphs:
        is_heading = para.style is not None and para.style.name.lower().startswith("heading")
        if is_heading and buf:
            out.append(Segment(f"§{section}", _clean("\n".join(buf))))
            section, buf = section + 1, []
        if para.text.strip():
            buf.append(para.text)
    if buf:
        out.append(Segment(f"§{section}", _clean("\n".join(buf))))
    return [s for s in out if s.text]
