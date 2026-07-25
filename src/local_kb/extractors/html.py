"""Offline HTML extraction with active and navigational content removed."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from .base import Extraction, ExtractionError, Fragment, registry, require_regular_file


def extract_html(path: Path) -> Extraction:
    candidate = require_regular_file(path)
    try:
        raw_html = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ExtractionError(f"failed to read HTML file: {candidate}") from exc
    soup = BeautifulSoup(raw_html, "html.parser")
    for node in soup(["script", "style", "nav"]):
        node.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else candidate.stem
    body = soup.get_text("\n", strip=True)
    return Extraction("extracted", [Fragment(f"title:{title}", body)])


class HtmlExtractor:
    suffixes = {".html", ".htm"}
    extract = staticmethod(extract_html)


registry.register(HtmlExtractor())
