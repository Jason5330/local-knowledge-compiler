"""Text-only PDF extraction; scanned pages explicitly require OCR."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from .base import Extraction, ExtractionError, Fragment, registry, snapshot_file


def _extract_pdf_snapshot(path: Path) -> Extraction:
    try:
        reader = PdfReader(path)
        fragments = [
            Fragment(f"page:{index}", page.extract_text() or "")
            for index, page in enumerate(reader.pages, 1)
        ]
    except Exception as exc:
        raise ExtractionError(f"failed to read PDF file: {path}") from exc
    fragments = [fragment for fragment in fragments if fragment.text.strip()]
    if fragments:
        return Extraction("extracted", fragments)
    return Extraction("pending_extractor", [], "PDF has no extractable text; OCR required")


def extract_pdf(path: Path) -> Extraction:
    with snapshot_file(path) as snapshot:
        return _extract_pdf_snapshot(snapshot)


class PdfExtractor:
    suffixes = {".pdf"}
    extract = staticmethod(extract_pdf)
    extract_snapshot = staticmethod(_extract_pdf_snapshot)


registry.register(PdfExtractor())
