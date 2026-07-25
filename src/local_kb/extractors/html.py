"""Offline HTML extraction with active and navigational content removed."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from .base import (
    Extraction,
    ExtractionError,
    FragmentCollector,
    enforce_extraction_budget,
    registry,
    snapshot_file,
)


def _extract_html_snapshot(path: Path) -> Extraction:
    try:
        raw_html = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ExtractionError(f"failed to read HTML file: {path}") from exc
    soup = BeautifulSoup(raw_html, "html.parser")
    for node in soup(["script", "style", "nav"]):
        node.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else path.stem
    body = soup.get_text("\n", strip=True)
    collector = FragmentCollector()
    collector.append(f"title:{title}", body)
    return collector.extraction("extracted")


def extract_html(path: Path) -> Extraction:
    with snapshot_file(path) as snapshot:
        return enforce_extraction_budget(_extract_html_snapshot(snapshot))


class HtmlExtractor:
    suffixes = {".html", ".htm"}
    extract = staticmethod(extract_html)
    extract_snapshot = staticmethod(_extract_html_snapshot)


registry.register(HtmlExtractor())
