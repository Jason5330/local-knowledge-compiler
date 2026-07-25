"""UTF-8 local plain-text extraction."""

from __future__ import annotations

from pathlib import Path

from .base import Extraction, ExtractionError, Fragment, registry, require_regular_file


class TextExtractor:
    suffixes = {".txt", ".md", ".json", ".csv", ".py", ".js", ".ts"}

    def extract(self, path: Path) -> Extraction:
        candidate = require_regular_file(path)
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ExtractionError(f"failed to read text file: {candidate}") from exc
        return Extraction(
            "extracted",
            [
                Fragment(f"lines:{number}-{number}", line)
                for number, line in enumerate(text.splitlines(), 1)
                if line.strip()
            ],
        )


registry.register(TextExtractor())
