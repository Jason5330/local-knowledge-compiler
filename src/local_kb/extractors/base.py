"""Contracts and dispatch for local, non-executing document extractors."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Fragment:
    locator: str
    text: str


@dataclass(frozen=True)
class Extraction:
    status: str
    fragments: list[Fragment]
    warning: str | None = None


class ExtractionError(RuntimeError):
    """A supported local document could not safely be read."""


class Extractor(Protocol):
    suffixes: set[str] | frozenset[str]

    def extract(self, path: Path) -> Extraction: ...


def require_regular_file(path: Path) -> Path:
    """Reject non-local filesystem indirections before parsing a document."""
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError(f"extractor input must not be a symbolic link: {candidate}")
    is_junction = getattr(candidate, "is_junction", None)
    if is_junction is not None and is_junction():
        raise ValueError(f"extractor input must not be a junction: {candidate}")
    if not candidate.exists() or not candidate.is_file():
        raise ValueError(f"extractor input must be an existing regular file: {candidate}")
    return candidate


class Registry:
    def __init__(self) -> None:
        self._items: dict[str, Extractor] = {}

    def register(self, extractor: Extractor) -> None:
        suffixes = {suffix.lower() for suffix in extractor.suffixes}
        duplicates = sorted(suffix for suffix in suffixes if suffix in self._items)
        if duplicates:
            raise ValueError(f"duplicate extractor suffix: {duplicates[0]}")
        self._items.update({suffix: extractor for suffix in suffixes})

    def extract(self, path: Path) -> Extraction:
        candidate = require_regular_file(path)
        suffix = candidate.suffix.lower()
        extractor = self._items.get(suffix)
        if extractor is not None:
            return extractor.extract(candidate)
        from .unsupported import pending_extractor

        return pending_extractor(candidate)


registry = Registry()
