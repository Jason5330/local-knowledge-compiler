"""Text-only PDF extraction; scanned pages explicitly require OCR."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from .base import (
    Extraction,
    ExtractionError,
    FragmentCollector,
    enforce_extraction_budget,
    registry,
    snapshot_file,
)


def _extract_pdf_snapshot(path: Path) -> Extraction:
    collector = FragmentCollector()
    try:
        with path.open("rb") as stream:
            reader = PdfReader(stream)
            for index, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                if text.strip():
                    collector.append(f"page:{index}", text)
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"failed to read PDF file: {path}") from exc
    extraction = collector.extraction("extracted")
    if extraction.fragments:
        return extraction
    return collector.extraction(
        "pending_extractor", "PDF has no extractable text; OCR required"
    )


def extract_pdf(path: Path) -> Extraction:
    with snapshot_file(path) as snapshot:
        return enforce_extraction_budget(_extract_pdf_snapshot(snapshot))


class PdfExtractor:
    suffixes = {".pdf"}
    extract = staticmethod(extract_pdf)
    extract_snapshot = staticmethod(_extract_pdf_snapshot)


registry.register(PdfExtractor())
