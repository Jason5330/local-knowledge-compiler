"""Explicit safe downgrade for formats without an installed extractor."""

from __future__ import annotations

from pathlib import Path

from .base import Extraction


def pending_extractor(path: Path) -> Extraction:
    """Represent an unhandled local format without inventing its contents."""
    suffix = path.suffix.lower()
    return Extraction(
        "pending_extractor",
        [],
        f"no extractor for {suffix or 'no suffix'}",
    )


class UnsupportedExtractor:
    """Optional explicit extractor; intentionally never registered globally."""

    suffixes: set[str] = set()

    def extract(self, path: Path) -> Extraction:
        return pending_extractor(path)
