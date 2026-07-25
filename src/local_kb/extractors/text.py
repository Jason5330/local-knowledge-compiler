"""UTF-8 local plain-text extraction."""

from __future__ import annotations

from pathlib import Path

from .base import Extraction, ExtractionError, Fragment, registry, snapshot_file


def _extract_text_snapshot(path: Path) -> Extraction:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ExtractionError(f"failed to read text file: {path}") from exc
    return Extraction(
        "extracted",
        [
            Fragment(f"lines:{number}-{number}", line)
            for number, line in enumerate(text.splitlines(), 1)
            if line.strip()
        ],
    )


class TextExtractor:
    suffixes = {".txt", ".md", ".json", ".csv", ".py", ".js", ".ts"}

    def extract(self, path: Path) -> Extraction:
        with snapshot_file(path) as snapshot:
            return _extract_text_snapshot(snapshot)

    extract_snapshot = staticmethod(_extract_text_snapshot)


registry.register(TextExtractor())
