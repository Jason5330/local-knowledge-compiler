"""Text-only PDF extraction; scanned pages explicitly require OCR."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from .base import Extraction, ExtractionError, Fragment, registry, require_regular_file


def extract_pdf(path: Path) -> Extraction:
    candidate = require_regular_file(path)
    try:
        reader = PdfReader(candidate)
        fragments = [
            Fragment(f"page:{index}", page.extract_text() or "")
            for index, page in enumerate(reader.pages, 1)
        ]
    except Exception as exc:
        raise ExtractionError(f"failed to read PDF file: {candidate}") from exc
    fragments = [fragment for fragment in fragments if fragment.text.strip()]
    if fragments:
        return Extraction("extracted", fragments)
    return Extraction("pending_extractor", [], "PDF has no extractable text; OCR required")


class PdfExtractor:
    suffixes = {".pdf"}
    extract = staticmethod(extract_pdf)


registry.register(PdfExtractor())
