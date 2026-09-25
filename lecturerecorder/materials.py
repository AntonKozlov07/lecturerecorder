"""Reading course materials (PDFs, slides, documents, notes, photos) into text.

Text is what Claude reads for most files. PDFs without a text layer (scans)
and photos are sent to Claude as the original file instead, since it can read
pages and images directly.
"""

from __future__ import annotations

from pathlib import Path

PDF_EXT = {".pdf"}
SLIDES_EXT = {".pptx"}
DOC_EXT = {".docx"}
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tex", ".rtf"}
IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}

ACCEPTED = sorted(PDF_EXT | SLIDES_EXT | DOC_EXT | TEXT_EXT | set(IMAGE_EXT))
MAX_IMAGE_BYTES = 5 * 1024 * 1024       # API limit per image
MAX_SCAN_BYTES = 30 * 1024 * 1024       # keeps a scanned PDF inside one request


class MaterialError(Exception):
    pass


def extract(path: Path) -> tuple[str, str, int]:
    """Return (kind, text, pages) for a saved upload."""
    ext = path.suffix.lower()
    if ext in PDF_EXT:
        return _pdf(path)
    if ext in SLIDES_EXT:
        return _pptx(path)
    if ext in DOC_EXT:
        return _docx(path)
    if ext in TEXT_EXT:
        text = path.read_bytes().decode("utf-8", errors="replace").strip()
        return "text", text, 0
    if ext in IMAGE_EXT:
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise MaterialError("Images must be 5 MB or smaller. Try a screenshot or a lower-resolution photo.")
        return "image", "", 1
    if ext in {".ppt", ".doc"}:
        raise MaterialError(f"Old {ext} files are not supported. Save it as {ext}x or PDF and upload that.")
    raise MaterialError(f"Unsupported file type {ext or '(none)'}. Use PDF, PowerPoint, Word, text or an image.")


def _pdf(path: Path) -> tuple[str, str, int]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            reader.decrypt("")
        pages = []
        for i, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"[Page {i}]\n{text}")
    except Exception as exc:
        raise MaterialError(f"Could not read this PDF: {exc}") from exc
    count = len(reader.pages)
    text = "\n\n".join(pages)
    # Scanned PDFs have little or no text layer; Claude reads those as page images.
    if len(text) < 200 * max(1, count) * 0.25:
        if path.stat().st_size > MAX_SCAN_BYTES:
            raise MaterialError("This PDF looks scanned (no selectable text) and is over 30 MB. "
                                "Split it into smaller files or run it through OCR first.")
        return "pdf_scan", "", count
    return "pdf", text, count


def _pptx(path: Path) -> tuple[str, str, int]:
    from pptx import Presentation

    try:
        deck = Presentation(str(path))
    except Exception as exc:
        raise MaterialError(f"Could not read these slides: {exc}") from exc
    slides = []
    for i, slide in enumerate(deck.slides, 1):
        lines = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                lines += [p.text for p in shape.text_frame.paragraphs if p.text.strip()]
            elif getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    lines.append(" | ".join(cell.text.strip() for cell in row.cells))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"Speaker notes: {notes}")
        if lines:
            slides.append(f"[Slide {i}]\n" + "\n".join(lines))
    return "slides", "\n\n".join(slides), len(deck.slides)


def _docx(path: Path) -> tuple[str, str, int]:
    import docx

    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise MaterialError(f"Could not read this document: {exc}") from exc
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "document", "\n".join(parts), 0


def estimate_tokens(material: dict) -> int:
    """Rough token count used to warn before sending a very large course."""
    if material["kind"] == "pdf_scan":
        return material["pages"] * 1600
    if material["kind"] == "image":
        return 1600
    return material.get("chars", len(material.get("text", ""))) // 4
