"""Read-only vault health checks and atomic catalog rebuilding."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import uuid4

from .catalog import Catalog
from .extractors import registry as extractor_registry
from .models import SourceVersion
from .paths import VaultPaths
from .queue import WriterLock
from .query import (
    _frontmatter_list,
    _open_pinned_regular,
    _pinned_directory,
    _safe_existing_tree,
    _safe_walk_wiki,
)
from .source_store import SourceStore


MAX_CACHE_FILES = 100_000
MAX_WIKI_BYTES = 4 * 1024 * 1024
MAX_QUEUE_ENTRIES = 10_000


def rebuild_catalog(vault: VaultPaths | Path | str) -> int:
    """Build a fresh catalog from verified cache files, then publish atomically."""
    paths = _paths(vault)
    with WriterLock(paths.runtime / "write.lock", timeout=0):
        return _rebuild_catalog_unlocked(paths)


def _rebuild_catalog_unlocked(paths: VaultPaths) -> int:
    paths.index.mkdir(parents=True, exist_ok=True)
    records = _read_raw_records(paths)
    ordered = _order_lineage(records)
    target = paths.index / "catalog.sqlite3"
    temporary = paths.index / f".catalog-rebuild-{uuid4().hex}.sqlite3"
    catalog = Catalog(temporary)
    try:
        catalog.initialize()
        for source, fragments in ordered:
            catalog.upsert_source(source, fragments)
        with closing(sqlite3.connect(temporary)) as connection:
            with connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("PRAGMA journal_mode=DELETE")
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("rebuilt catalog failed integrity check")
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _sync_directory(paths.index)
        return len(ordered)
    except BaseException:
        raise
    finally:
        for candidate in (
            temporary,
            Path(f"{temporary}-wal"),
            Path(f"{temporary}-shm"),
        ):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass


def _read_raw_records(
    paths: VaultPaths,
) -> list[tuple[SourceVersion, list[tuple[str, str]]]]:
    """Re-extract the catalog exclusively from immutable, verified raw data."""
    if not paths.raw.is_dir():
        return []
    store = SourceStore(paths.raw)
    records: list[tuple[SourceVersion, list[tuple[str, str]]]] = []
    entry_count = 0
    for space_dir in sorted(paths.raw.iterdir(), key=lambda path: path.name.casefold()):
        entry_count += 1
        if entry_count > MAX_CACHE_FILES:
            raise ValueError("raw store contains too many entries")
        if space_dir.name.startswith("."):
            continue
        _require_plain_directory(paths.raw, space_dir)
        for source_dir in sorted(space_dir.iterdir(), key=lambda path: path.name.casefold()):
            entry_count += 1
            if entry_count > MAX_CACHE_FILES:
                raise ValueError("raw store contains too many entries")
            _require_plain_directory(paths.raw, source_dir)
            for version_dir in sorted(source_dir.iterdir(), key=lambda path: path.name.casefold()):
                entry_count += 1
                if entry_count > MAX_CACHE_FILES:
                    raise ValueError("raw store contains too many entries")
                if version_dir.name.startswith("."):
                    continue
                _require_plain_directory(paths.raw, version_dir)
                manifest = version_dir / "manifest.json"
                source = store._read_manifest(manifest)
                content = version_dir / source.original_name
                extraction = extractor_registry.extract(content)
                indexed = replace(source, status=extraction.status)
                records.append(
                    (
                        indexed,
                        [(fragment.locator, fragment.text) for fragment in extraction.fragments],
                    )
                )
    return records


def _require_plain_directory(root: Path, candidate: Path) -> None:
    _safe_existing_tree(root, candidate)
    info = candidate.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or candidate.is_symlink()
        or bool(getattr(info, "st_file_attributes", 0) & 0x400)
    ):
        raise ValueError("raw store contains an unsafe directory")


def lint(vault: VaultPaths | Path | str) -> dict[str, object]:
    """Return a bounded, read-only health report."""
    paths = _paths(vault)
    catalog_rows, catalog_error = _catalog_inventory(paths.index / "catalog.sqlite3")
    catalog_ids = {row["source_id"] for row in catalog_rows.values()}
    raw_rows, raw_error = _raw_inventory(paths)
    missing: list[str] = []
    pages: list[dict[str, object]] = []
    wiki_pages = 0
    scan_state = {"truncated": False}
    if paths.wiki.exists():
        for page, parent_fd, name in _safe_walk_wiki(paths.wiki, scan_state):
            wiki_pages += 1
            page_path = page.relative_to(paths.root).as_posix()
            try:
                text = _read_text(page, parent_fd, name, MAX_WIKI_BYTES)
                header = _frontmatter(text)
                source_ids = _frontmatter_list(header, "source_ids")
                page = {
                    "path": page_path,
                    "id": _frontmatter_value(header, "id"),
                    "title": _frontmatter_value(header, "title"),
                    "aliases": _frontmatter_list(header, "aliases"),
                    "related": _markdown_list_section(text, "Related"),
                    "status": _frontmatter_value(header, "status"),
                    "source_ids": source_ids,
                }
                pages.append(page)
                if not source_ids or any(source_id not in catalog_ids for source_id in source_ids):
                    missing.append(str(page["path"]))
            except (OSError, UnicodeError, ValueError):
                missing.append(page_path)
    pending, queue_truncated = _count_pending(paths)
    missing.sort()
    issues = _wiki_issues(pages)
    index_raw: list[str] = []
    for version_id in sorted(set(raw_rows) | set(catalog_rows)):
        raw = raw_rows.get(version_id)
        indexed = catalog_rows.get(version_id)
        if raw is None:
            index_raw.append(f"catalog_only:{version_id}")
        elif indexed is None:
            index_raw.append(f"raw_only:{version_id}")
        elif any(str(raw[key]) != str(indexed[key]) for key in ("source_id", "space", "relative_path", "sha256")):
            index_raw.append(f"metadata:{version_id}")
    if catalog_error:
        index_raw.append("catalog_unavailable_or_invalid")
    if raw_error:
        index_raw.append("raw_inventory_invalid")
    issues["index_raw_mismatches"] = index_raw
    issues["index_wiki_mismatches"] = sorted(set(missing))
    all_issues = any(bool(value) for value in issues.values())
    truncated = bool(scan_state["truncated"] or queue_truncated)
    return {
        "wiki_pages": wiki_pages,
        "missing_source_pages": missing,
        "pending_jobs": pending,
        "catalog_source_ids": sorted(catalog_ids),
        "issues": issues,
        "truncated": truncated,
        "healthy": not all_issues and not truncated,
    }


def _frontmatter_value(header: str, key: str) -> str:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*(.+?)\s*$", header)
    if match is None:
        return ""
    value = match.group(1)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value.strip()
    return parsed if isinstance(parsed, str) else ""


def _markdown_list_section(text: str, heading: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    marker = f"## {heading}\n"
    body = text.partition(marker)[2]
    if not body:
        return []
    section = body.partition("\n## ")[0]
    return [
        line[2:].strip()
        for line in section.splitlines()
        if line.startswith("- ") and line[2:].strip() not in {"無", "none"}
    ][:32]


def _wiki_issues(pages: list[dict[str, object]]) -> dict[str, list[object]]:
    ids: dict[str, list[str]] = {}
    aliases: dict[str, list[str]] = {}
    known: set[str] = set()
    for page in pages:
        path = str(page["path"])
        page_id = str(page["id"])
        title = str(page["title"])
        if page_id:
            ids.setdefault(page_id.casefold(), []).append(path)
        for value in (page_id, title, path, Path(path).stem):
            if value:
                known.add(value.casefold())
        for alias in page["aliases"]:
            aliases.setdefault(str(alias).casefold(), []).append(path)
            known.add(str(alias).casefold())
    duplicate_ids = [
        {"value": key, "pages": sorted(paths)}
        for key, paths in sorted(ids.items()) if len(paths) > 1
    ]
    duplicate_aliases = [
        {"value": key, "pages": sorted(paths)}
        for key, paths in sorted(aliases.items()) if len(paths) > 1
    ]
    broken: list[dict[str, str]] = []
    inbound: set[str] = set()
    for page in pages:
        for related in page["related"]:
            key = str(related).casefold()
            if key in known:
                inbound.add(key)
            else:
                broken.append({"page": str(page["path"]), "target": str(related)})
    orphan = []
    for page in pages:
        keys = {
            str(page["id"]).casefold(), str(page["title"]).casefold(),
            str(page["path"]).casefold(), Path(str(page["path"])).stem.casefold(),
        }
        valid_outgoing = any(str(item).casefold() in known for item in page["related"])
        if not valid_outgoing and not keys.intersection(inbound):
            orphan.append(str(page["path"]))
    return {
        "duplicate_page_ids": duplicate_ids,
        "duplicate_aliases": duplicate_aliases,
        "broken_related": sorted(broken, key=lambda item: (item["page"], item["target"])),
        "orphan_pages": sorted(orphan),
        "stale_pages": sorted(
            str(page["path"]) for page in pages if page["status"] == "stale"
        ),
        "outdated_versions": sorted(
            str(page["path"]) for page in pages if page["status"] == "stale"
        ),
    }


def _catalog_inventory(path: Path) -> tuple[dict[str, dict[str, object]], bool]:
    if not path.is_file():
        return {}, True
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)) as connection:
            rows = connection.execute(
                "SELECT version_id, source_id, space, relative_path, sha256 FROM sources"
            ).fetchall()
        return {
            row[0]: {
                "source_id": row[1], "space": row[2],
                "relative_path": row[3], "sha256": row[4],
            }
            for row in rows
        }, False
    except sqlite3.Error:
        return {}, True


def _raw_inventory(paths: VaultPaths) -> tuple[dict[str, dict[str, object]], bool]:
    if not paths.raw.is_dir():
        return {}, False
    store = SourceStore(paths.raw)
    result: dict[str, dict[str, object]] = {}
    count = 0
    scanned = 0
    try:
        for space_dir in sorted(paths.raw.iterdir()):
            scanned += 1
            if scanned > MAX_CACHE_FILES:
                return result, True
            if space_dir.name.startswith("."):
                continue
            _require_plain_directory(paths.raw, space_dir)
            for source_dir in sorted(space_dir.iterdir()):
                scanned += 1
                if scanned > MAX_CACHE_FILES:
                    return result, True
                _require_plain_directory(paths.raw, source_dir)
                for version_dir in sorted(source_dir.iterdir()):
                    scanned += 1
                    if scanned > MAX_CACHE_FILES:
                        return result, True
                    if version_dir.name.startswith("."):
                        continue
                    count += 1
                    if count > MAX_CACHE_FILES:
                        return result, True
                    _require_plain_directory(paths.raw, version_dir)
                    source = store._read_manifest(version_dir / "manifest.json")
                    result[source.version_id] = {
                        "source_id": source.source_id, "space": source.space,
                        "relative_path": source.relative_path, "sha256": source.sha256,
                    }
    except (OSError, ValueError):
        return result, True
    return result, False


def _paths(vault: VaultPaths | Path | str) -> VaultPaths:
    paths = vault if isinstance(vault, VaultPaths) else VaultPaths(Path(vault).absolute())
    root = paths.root.absolute()
    _safe_existing_tree(root, root)
    if not root.is_dir():
        raise ValueError("vault root is unavailable")
    return VaultPaths(root)


def _order_lineage(
    records: list[tuple[SourceVersion, list[tuple[str, str]]]]
) -> list[tuple[SourceVersion, list[tuple[str, str]]]]:
    pending = {source.version_id: (source, fragments) for source, fragments in records}
    result: list[tuple[SourceVersion, list[tuple[str, str]]]] = []
    emitted: set[str] = set()
    while pending:
        ready = sorted(
            version
            for version, (source, _) in pending.items()
            if source.previous_version_id is None or source.previous_version_id in emitted
        )
        if not ready:
            raise ValueError("cache lineage is incomplete or cyclic")
        for version in ready:
            source, fragments = pending.pop(version)
            if source.previous_version_id is not None:
                predecessor = next(
                    item[0] for item in result if item[0].version_id == source.previous_version_id
                )
                if predecessor.source_id != source.source_id:
                    raise ValueError("cache lineage crosses source IDs")
            result.append((source, fragments))
            emitted.add(version)
    return result


def _read_bytes(path: Path, parent_fd: int | None, name: str, limit: int) -> bytes:
    with _open_pinned_regular(path, parent_fd=parent_fd, name=name) as (descriptor, _):
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining and (chunk := os.read(descriptor, min(65_536, remaining))):
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
    if len(encoded) > limit:
        raise ValueError("cache file exceeds size limit")
    return encoded


def _read_text(path: Path, parent_fd: int | None, name: str, limit: int) -> str:
    return _read_bytes(path, parent_fd, name, limit).decode("utf-8")


def _frontmatter(text: str) -> str:
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        raise ValueError("wiki page has no frontmatter")
    end = normalized.find("\n---\n", 4)
    if end < 0:
        raise ValueError("wiki page has invalid frontmatter")
    return normalized[4:end]


def _count_pending(paths: VaultPaths) -> tuple[int, bool]:
    if not paths.queue.exists():
        return 0, False
    _safe_existing_tree(paths.root, paths.queue)
    count = 0
    scanned = 0
    truncated = False
    with _pinned_directory(paths.root, paths.queue) as parent_fd:
        target = parent_fd if parent_fd is not None else paths.queue
        with os.scandir(target) as entries:
            for entry in entries:
                scanned += 1
                if scanned > MAX_QUEUE_ENTRIES:
                    truncated = True
                    break
                if entry.name.endswith(".json"):
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and not entry.is_symlink():
                        try:
                            encoded = _read_bytes(
                                paths.queue / entry.name,
                                parent_fd,
                                entry.name,
                                4 * 1024 * 1024,
                            )
                            payload = json.loads(encoded.decode("utf-8"))
                            if (
                                isinstance(payload, dict)
                                and payload.get("state")
                                not in {"published", "pending_attention"}
                            ):
                                count += 1
                        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                            # A corrupt queue entry needs attention and is
                            # therefore counted as pending without mutating it.
                            count += 1
    return count, truncated


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
