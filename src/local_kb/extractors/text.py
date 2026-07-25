"""UTF-8 local plain-text extraction."""

from __future__ import annotations

from pathlib import Path

from . import base as limits
from .base import (
    Extraction,
    ExtractionError,
    FragmentCollector,
    enforce_extraction_budget,
    registry,
    snapshot_file,
)


TEXT_CHUNK_CHARACTERS = 64 * 1024
_LINE_BOUNDARIES = {
    "\n",
    "\r",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
}


def _extract_text_snapshot(path: Path) -> Extraction:
    collector = FragmentCollector()
    line_parts: list[str] = []
    line_length = 0
    line_number = 1
    skip_lf_after_cr = False

    def append_segment(segment: str) -> None:
        nonlocal line_length
        next_length = line_length + len(segment)
        if next_length > limits.MAX_EXTRACTION_CHARACTERS:
            raise ExtractionError(
                "text unbroken line exceeds extraction character count budget"
            )
        if segment:
            line_parts.append(segment)
            line_length = next_length

    def finish_line() -> None:
        nonlocal line_length, line_number
        line = "".join(line_parts)
        if line.strip():
            collector.append(f"lines:{line_number}-{line_number}", line)
        line_parts.clear()
        line_length = 0
        line_number += 1

    try:
        with path.open(
            "r", encoding="utf-8", errors="replace", newline=""
        ) as stream:
            while chunk := stream.read(TEXT_CHUNK_CHARACTERS):
                segment_start = 0
                for index, character in enumerate(chunk):
                    if skip_lf_after_cr:
                        skip_lf_after_cr = False
                        if character == "\n":
                            segment_start = index + 1
                            continue
                    if character not in _LINE_BOUNDARIES:
                        continue
                    append_segment(chunk[segment_start:index])
                    finish_line()
                    skip_lf_after_cr = character == "\r"
                    segment_start = index + 1
                append_segment(chunk[segment_start:])
    except OSError as exc:
        raise ExtractionError(f"failed to read text file: {path}") from exc
    if line_parts:
        finish_line()
    return collector.extraction("extracted")


class TextExtractor:
    suffixes = {".txt", ".md", ".json", ".csv", ".py", ".js", ".ts"}

    def extract(self, path: Path) -> Extraction:
        with snapshot_file(path) as snapshot:
            return enforce_extraction_budget(_extract_text_snapshot(snapshot))

    extract_snapshot = staticmethod(_extract_text_snapshot)


registry.register(TextExtractor())
