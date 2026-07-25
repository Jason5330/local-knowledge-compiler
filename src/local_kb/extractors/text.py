"""UTF-8 local plain-text extraction."""

from __future__ import annotations

from pathlib import Path

from .base import (
    Extraction,
    ExtractionError,
    FragmentCollector,
    enforce_extraction_budget,
    registry,
    snapshot_file,
)


def _extract_text_snapshot(path: Path) -> Extraction:
    collector = FragmentCollector()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream, 1):
                line = line.rstrip("\r\n")
                if line.strip():
                    collector.append(f"lines:{number}-{number}", line)
    except OSError as exc:
        raise ExtractionError(f"failed to read text file: {path}") from exc
    return collector.extraction("extracted")


class TextExtractor:
    suffixes = {".txt", ".md", ".json", ".csv", ".py", ".js", ".ts"}

    def extract(self, path: Path) -> Extraction:
        with snapshot_file(path) as snapshot:
            return enforce_extraction_budget(_extract_text_snapshot(snapshot))

    extract_snapshot = staticmethod(_extract_text_snapshot)


registry.register(TextExtractor())
