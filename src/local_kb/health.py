"""Read-only vault health checks and atomic catalog rebuilding."""

from __future__ import annotations

from contextlib import closing
from dataclasses import fields
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import uuid4

from .catalog import Catalog
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
MAX_CACHE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_CACHE_BYTES = 256 * 1024 * 1024
MAX_WIKI_BYTES = 4 * 1024 * 1024
MAX_QUEUE_ENTRIES = 10_000
_CACHE_NAME = re.compile(r"ver_[0-9a-f]{64}\.json\Z")


def rebuild_catalog(vault: VaultPaths | Path | str) -> int:
    """Build a fresh catalog from verified cache files, then publish atomically."""
    paths = _paths(vault)
    with WriterLock(paths.runtime / "write.lock", timeout=0):
        return _rebuild_catalog_unlocked(paths)


def _rebuild_catalog_unlocked(paths: VaultPaths) -> int:
    paths.index.mkdir(parents=True, exist_ok=True)
    cache = paths.index / "cache"
    records = _read_cache_records(paths, cache)
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


def lint(vault: VaultPaths | Path | str) -> dict[str, object]:
    """Return a bounded, read-only health report."""
    paths = _paths(vault)
    catalog_ids = _catalog_source_ids(paths.index / "catalog.sqlite3")
    missing: list[str] = []
    wiki_pages = 0
    scan_state = {"truncated": False}
    if paths.wiki.exists():
        for page, parent_fd, name in _safe_walk_wiki(paths.wiki, scan_state):
            wiki_pages += 1
            try:
                text = _read_text(page, parent_fd, name, MAX_WIKI_BYTES)
                header = _frontmatter(text)
                source_ids = _frontmatter_list(header, "source_ids")
                if not source_ids or any(source_id not in catalog_ids for source_id in source_ids):
                    missing.append(page.relative_to(paths.root).as_posix())
            except (OSError, UnicodeError, ValueError):
                missing.append(page.relative_to(paths.root).as_posix())
    pending, queue_truncated = _count_pending(paths)
    missing.sort()
    return {
        "wiki_pages": wiki_pages,
        "missing_source_pages": missing,
        "pending_jobs": pending,
        "catalog_source_ids": sorted(catalog_ids),
        "truncated": bool(scan_state["truncated"] or queue_truncated),
        "healthy": not missing and not scan_state["truncated"] and not queue_truncated,
    }


def _paths(vault: VaultPaths | Path | str) -> VaultPaths:
    paths = vault if isinstance(vault, VaultPaths) else VaultPaths(Path(vault).absolute())
    root = paths.root.absolute()
    _safe_existing_tree(root, root)
    if not root.is_dir():
        raise ValueError("vault root is unavailable")
    return VaultPaths(root)


def _read_cache_records(
    paths: VaultPaths, cache: Path
) -> list[tuple[SourceVersion, list[tuple[str, str]]]]:
    if not cache.exists():
        return []
    _safe_existing_tree(paths.root, cache)
    records: list[tuple[SourceVersion, list[tuple[str, str]]]] = []
    total = 0
    seen_versions: set[str] = set()
    with _pinned_directory(paths.root, cache) as parent_fd:
        target = parent_fd if parent_fd is not None else cache
        with os.scandir(target) as entries:
            names = sorted(entry.name for entry in entries)
        if len(names) > MAX_CACHE_FILES:
            raise ValueError("cache contains too many entries")
        for name in names:
            if not _CACHE_NAME.fullmatch(name):
                if name.startswith(".") and name.endswith(".tmp"):
                    continue
                raise ValueError("cache contains an unexpected entry")
            path = cache / name
            try:
                encoded = _read_bytes(path, parent_fd, name, MAX_CACHE_BYTES)
                total += len(encoded)
                if total > MAX_TOTAL_CACHE_BYTES:
                    raise ValueError("cache exceeds total size limit")
                payload = json.loads(encoded.decode("utf-8"))
                source, fragments = _validate_cache_payload(paths, name, payload)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
                if isinstance(error, ValueError) and str(error).startswith("cache "):
                    raise
                raise ValueError(f"cache file is invalid: {name}") from error
            if source.version_id in seen_versions:
                raise ValueError("cache contains duplicate versions")
            seen_versions.add(source.version_id)
            records.append((source, fragments))
    return records


def _validate_cache_payload(
    paths: VaultPaths, name: str, payload: object
) -> tuple[SourceVersion, list[tuple[str, str]]]:
    if not isinstance(payload, dict) or set(payload) != {"source", "fragments", "warning"}:
        raise ValueError("cache payload has invalid fields")
    source_data = payload["source"]
    if not isinstance(source_data, dict) or set(source_data) != {
        field.name for field in fields(SourceVersion)
    }:
        raise ValueError("cache source has invalid fields")
    try:
        source = SourceVersion(**source_data)
    except TypeError as error:
        raise ValueError("cache source is invalid") from error
    if name != f"{source.version_id}.json":
        raise ValueError("cache filename does not match version")
    raw = paths.root / source.relative_path
    try:
        raw.relative_to(paths.raw)
    except ValueError as error:
        raise ValueError("cache source path leaves raw storage") from error
    archived = SourceVersion(
        source_id=source.source_id,
        version_id=source.version_id,
        space=source.space,
        original_name=source.original_name,
        relative_path=source.relative_path,
        sha256=source.sha256,
        media_type=source.media_type,
        status="archived",
        previous_version_id=source.previous_version_id,
        created_sequence=source.created_sequence,
    )
    SourceStore(paths.raw)._validate_manifest(archived, raw.parent)
    fragments_data = payload["fragments"]
    if not isinstance(fragments_data, list) or len(fragments_data) > 100_000:
        raise ValueError("cache fragments are invalid")
    fragments: list[tuple[str, str]] = []
    for item in fragments_data:
        if (
            not isinstance(item, dict)
            or set(item) != {"locator", "text"}
            or not isinstance(item["locator"], str)
            or not isinstance(item["text"], str)
            or not item["locator"]
            or len(item["locator"]) > 1024
            or len(item["text"]) > 1_000_000
        ):
            raise ValueError("cache fragment is invalid")
        fragments.append((item["locator"], item["text"]))
    return source, fragments


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


def _catalog_source_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        with closing(
            sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True
            )
        ) as connection:
            return {row[0] for row in connection.execute("SELECT DISTINCT source_id FROM sources")}
    except sqlite3.Error:
        return set()


def _count_pending(paths: VaultPaths) -> tuple[int, bool]:
    if not paths.queue.exists():
        return 0, False
    _safe_existing_tree(paths.root, paths.queue)
    count = 0
    truncated = False
    with _pinned_directory(paths.root, paths.queue) as parent_fd:
        target = parent_fd if parent_fd is not None else paths.queue
        with os.scandir(target) as entries:
            for entry in entries:
                if count >= MAX_QUEUE_ENTRIES:
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
