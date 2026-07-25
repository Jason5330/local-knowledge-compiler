"""Read-only vault health checks and atomic catalog rebuilding."""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
from uuid import uuid4

from .catalog import Catalog
from .extractors import registry as extractor_registry
from .extractors.base import ExtractionError, SnapshotCleanupError
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
MAX_CATALOG_FILE_BYTES = 256 * 1024 * 1024


class CatalogSnapshotUnavailable(RuntimeError):
    """The live catalog could not be snapshotted without touching its files."""


class CatalogBusy(CatalogSnapshotUnavailable):
    """A live WAL/SHM requires mutable SQLite reader locks; retry later."""


def rebuild_catalog(vault: VaultPaths | Path | str) -> int:
    """Build a fresh catalog from verified cache files, then publish atomically."""
    paths = _paths(vault)
    with WriterLock(paths.runtime / "write.lock", timeout=0):
        return _rebuild_catalog_unlocked(paths)


def _rebuild_catalog_unlocked(paths: VaultPaths) -> int:
    _safe_existing_tree(paths.root, paths.index)
    if (
        not paths.index.is_dir()
        or paths.index.is_symlink()
        or bool(getattr(paths.index.lstat(), "st_file_attributes", 0) & 0x400)
    ):
        raise ValueError("index directory is unsafe")
    records = _read_raw_records(paths)
    ordered = _order_lineage(records)
    target = paths.index / "catalog.sqlite3"
    with tempfile.TemporaryDirectory(prefix="local-kb-rebuild-") as workspace:
        built = Path(workspace) / "catalog.sqlite3"
        catalog = Catalog(built)
        catalog.initialize()
        for source, fragments in ordered:
            catalog.upsert_source(source, fragments)
        with closing(sqlite3.connect(built)) as connection:
            with connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("PRAGMA journal_mode=DELETE")
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("rebuilt catalog failed integrity check")
        with built.open("r+b") as stream:
            os.fsync(stream.fileno())
        with _pinned_directory(paths.root, paths.index) as parent_fd:
            _quiesce_catalog_sidecars(target, parent_fd)
            _publish_rebuilt_catalog(built, target, parent_fd)
        return len(ordered)


def _quiesce_catalog_sidecars(target: Path, parent_fd: int | None) -> None:
    sidecars = (Path(f"{target}-wal"), Path(f"{target}-shm"))
    observed: list[tuple[Path, os.stat_result]] = []
    for sidecar in sidecars:
        try:
            info = (
                os.stat(sidecar.name, dir_fd=parent_fd, follow_symlinks=False)
                if parent_fd is not None else sidecar.lstat()
            )
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CatalogBusy("catalog sidecar is unsafe")
        observed.append((sidecar, info))
    if any(info.st_size for _, info in observed):
        bound_target = _bound_catalog_path(target, parent_fd)
        try:
            with closing(sqlite3.connect(bound_target, timeout=0)) as connection:
                result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if result is not None and int(result[0]) != 0:
                    raise CatalogBusy("catalog WAL is busy")
                mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                if str(mode).casefold() != "delete":
                    raise CatalogBusy("catalog journal mode is busy")
        except sqlite3.OperationalError as error:
            raise CatalogBusy("catalog sidecars are live") from error
    for sidecar in sidecars:
        try:
            info = (
                os.stat(sidecar.name, dir_fd=parent_fd, follow_symlinks=False)
                if parent_fd is not None else sidecar.lstat()
            )
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0:
            raise CatalogBusy("catalog sidecar is not safely stale")
        if parent_fd is not None:
            os.unlink(sidecar.name, dir_fd=parent_fd)
        else:
            sidecar.unlink()
    for sidecar in sidecars:
        try:
            if parent_fd is not None:
                os.stat(sidecar.name, dir_fd=parent_fd, follow_symlinks=False)
            else:
                sidecar.lstat()
        except FileNotFoundError:
            continue
        raise CatalogBusy("catalog sidecar appeared during rebuild")


def _bound_catalog_path(target: Path, parent_fd: int | None) -> Path:
    if parent_fd is None:
        return target
    proc_path = Path(f"/proc/self/fd/{parent_fd}/{target.name}")
    if proc_path.parent.parent.is_dir():
        return proc_path
    raise CatalogBusy("cannot safely bind catalog path on this platform")


def _publish_rebuilt_catalog(
    built: Path, target: Path, parent_fd: int | None
) -> None:
    temporary_name = f".catalog-rebuild-{uuid4().hex}.sqlite3"
    temporary = target.parent / temporary_name
    descriptor: int | None = None
    try:
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        )
        descriptor = (
            os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
            if parent_fd is not None else os.open(temporary, flags, 0o600)
        )
        with built.open("rb") as source:
            while chunk := source.read(65_536):
                offset = 0
                while offset < len(chunk):
                    offset += os.write(descriptor, chunk[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            info = (
                os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
                if parent_fd is not None else target.lstat()
            )
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("catalog target is unsafe")
        except FileNotFoundError:
            pass
        if parent_fd is not None:
            os.replace(
                temporary_name, target.name,
                src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        else:
            os.replace(temporary, target)
            _sync_directory(target.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            if parent_fd is not None:
                os.unlink(temporary_name, dir_fd=parent_fd)
            else:
                temporary.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


def _read_raw_records(
    paths: VaultPaths, *, extraction_errors: list[dict[str, str]] | None = None,
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
                try:
                    extraction = extractor_registry.extract(content)
                except (
                    ExtractionError, SnapshotCleanupError,
                    OSError, UnicodeError, ValueError,
                ) as error:
                    if extraction_errors is None:
                        raise
                    if len(extraction_errors) < 100:
                        extraction_errors.append({
                            "version_id": source.version_id,
                            "error": error.__class__.__name__,
                        })
                    continue
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
    extraction_errors: list[dict[str, str]] = []
    try:
        raw_records = _read_raw_records(paths, extraction_errors=extraction_errors)
        raw_error = False
    except (OSError, ValueError):
        raw_records = []
        raw_error = True
    raw_rows = {
        source.version_id: {
            "source_id": source.source_id, "space": source.space,
            "relative_path": source.relative_path, "sha256": source.sha256,
        }
        for source, _ in raw_records
    }
    expected_fragments = {
        source.version_id: sorted(
            (locator, text) for locator, text in fragments if text.strip()
        )
        for source, fragments in raw_records
    }
    catalog_rows, catalog_fragments, catalog_fts, catalog_error = _catalog_inventory(
        paths.index / "catalog.sqlite3"
    )
    catalog_ids = {row["source_id"] for row in catalog_rows.values()}
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
    issues["raw_extraction_errors"] = extraction_errors
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
        index_raw.append(catalog_error)
    if raw_error:
        index_raw.append("raw_inventory_invalid")
    issues["index_raw_mismatches"] = index_raw
    content_mismatches: list[str] = []
    for version_id in sorted(set(expected_fragments) | set(catalog_rows)):
        expected = expected_fragments.get(version_id)
        actual = (
            catalog_fragments.get(version_id, [])
            if version_id in catalog_rows else None
        )
        if expected != actual:
            content_mismatches.append(f"fragments:{version_id}")
        source = raw_rows.get(version_id)
        expected_fts = (
            sorted(
                (
                    locator, source["source_id"], source["relative_path"],
                    source["space"], Catalog._searchable_text(text),
                )
                for locator, text in expected
            )
            if expected is not None and source is not None else None
        )
        actual_fts = catalog_fts.get(version_id, []) if version_id in catalog_rows else None
        if expected_fts != actual_fts:
            content_mismatches.append(f"fts:{version_id}")
    issues["index_content_mismatches"] = content_mismatches
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
    bindings: dict[str, list[dict[str, str]]] = {}
    for page in pages:
        path = str(page["path"])
        page_id = str(page["id"])
        title = str(page["title"])
        if page_id:
            ids.setdefault(page_id.casefold(), []).append(path)
        for kind, value in (("id", page_id), ("title", title)):
            if value:
                bindings.setdefault(value.casefold(), []).append(
                    {"kind": kind, "page": path}
                )
        for alias in page["aliases"]:
            aliases.setdefault(str(alias).casefold(), []).append(path)
            bindings.setdefault(str(alias).casefold(), []).append(
                {"kind": "alias", "page": path}
            )
    duplicate_ids = [
        {"value": key, "pages": sorted(paths)}
        for key, paths in sorted(ids.items()) if len(paths) > 1
    ]
    duplicate_aliases = [
        {"value": key, "pages": sorted(paths)}
        for key, paths in sorted(aliases.items()) if len(paths) > 1
    ]
    identity_collisions = []
    for value, entries in sorted(bindings.items()):
        if len({entry["page"] for entry in entries}) > 1:
            identity_collisions.append({
                "value": value,
                "bindings": sorted(entries, key=lambda item: (item["kind"], item["page"])),
            })
    broken: list[dict[str, str]] = []
    inbound_pages: set[str] = set()
    valid_outgoing_pages: set[str] = set()
    for page in pages:
        for related in page["related"]:
            key = str(related).casefold()
            targets = {entry["page"] for entry in bindings.get(key, [])}
            if len(targets) == 1:
                inbound_pages.update(targets)
                valid_outgoing_pages.add(str(page["path"]))
            else:
                broken.append({
                    "page": str(page["path"]),
                    "target": str(related),
                    "reason": "ambiguous" if targets else "missing",
                })
    orphan = []
    for page in pages:
        path = str(page["path"])
        if path not in valid_outgoing_pages and path not in inbound_pages:
            orphan.append(path)
    return {
        "duplicate_page_ids": duplicate_ids,
        "duplicate_aliases": duplicate_aliases,
        "identity_collisions": identity_collisions,
        "broken_related": sorted(broken, key=lambda item: (item["page"], item["target"])),
        "orphan_pages": sorted(orphan),
        "stale_pages": sorted(
            str(page["path"]) for page in pages if page["status"] == "stale"
        ),
        "outdated_versions": sorted(
            str(page["path"]) for page in pages if page["status"] == "stale"
        ),
    }


def _catalog_inventory(
    path: Path,
) -> tuple[
    dict[str, dict[str, object]],
    dict[str, list[tuple[str, str]]],
    dict[str, list[tuple[str, str, str, str, str]]],
    str | None,
]:
    if not path.is_file():
        return {}, {}, {}, "catalog_unavailable_or_invalid"
    try:
        # SQLite may create -shm even for mode=ro.  Inspect a bounded private
        # snapshot so lint observes WAL commits without changing the vault.
        with _catalog_snapshot(path) as snapshot:
            with closing(sqlite3.connect(snapshot)) as connection:
                connection.execute("PRAGMA query_only=ON")
                rows = connection.execute(
                    "SELECT version_id, source_id, space, relative_path, sha256 FROM sources"
                ).fetchall()
                fragment_rows = connection.execute(
                    "SELECT version_id, locator, text FROM source_fragments"
                ).fetchall()
                fts_rows = connection.execute(
                    """
                    SELECT map.version_id, map.locator, fts.source_id,
                           fts.relative_path, fts.space, fts.body
                    FROM source_fts_map AS map
                    JOIN source_fts AS fts ON fts.rowid = map.fts_rowid
                    """
                ).fetchall()
        sources = {
            row[0]: {
                "source_id": row[1], "space": row[2],
                "relative_path": row[3], "sha256": row[4],
            }
            for row in rows
        }
        fragments: dict[str, list[tuple[str, str]]] = {}
        for version_id, locator, text in fragment_rows:
            fragments.setdefault(version_id, []).append((locator, text))
        fts: dict[str, list[tuple[str, str, str, str, str]]] = {}
        for version_id, locator, source_id, relative_path, space, body in fts_rows:
            fts.setdefault(version_id, []).append(
                (locator, source_id, relative_path, space, body)
            )
        for values in fragments.values():
            values.sort()
        for values in fts.values():
            values.sort()
        return sources, fragments, fts, None
    except CatalogBusy:
        return {}, {}, {}, "catalog_busy"
    except CatalogSnapshotUnavailable:
        return {}, {}, {}, "catalog_snapshot_unavailable"
    except (OSError, ValueError, sqlite3.Error):
        return {}, {}, {}, "catalog_unavailable_or_invalid"


@contextmanager
def _catalog_snapshot(path: Path):
    with tempfile.TemporaryDirectory(prefix="local-kb-lint-") as directory:
        destination = Path(directory) / path.name
        try:
            before = _catalog_file_state(path)
        except (OSError, ValueError) as error:
            raise CatalogSnapshotUnavailable("catalog changed before snapshot") from error
        if any(
            name.endswith(("-wal", "-shm")) and token["size"]
            for name, token in before.items()
        ):
            raise CatalogBusy("catalog has a live WAL/SHM; retry after the writer stops")
        uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
        started = time.monotonic()
        with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as source:
            source.execute("PRAGMA query_only=ON")
            source.execute("BEGIN")
            source.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            page_size = int(source.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(source.execute("PRAGMA page_count").fetchone()[0])
            if page_size <= 0 or page_count < 0 or page_size * page_count > MAX_CATALOG_FILE_BYTES:
                raise ValueError("catalog exceeds snapshot size limit")

            def progress(_status: int, _remaining: int, total: int) -> None:
                if total * page_size > MAX_CATALOG_FILE_BYTES:
                    raise ValueError("catalog exceeds snapshot size limit")
                if time.monotonic() - started > 10:
                    raise TimeoutError("catalog snapshot timed out")

            with closing(sqlite3.connect(destination)) as target:
                source.backup(target, pages=256, progress=progress, sleep=0.01)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise CatalogSnapshotUnavailable("catalog snapshot failed integrity check")
            source.rollback()
        try:
            after = _catalog_file_state(path)
        except (OSError, ValueError) as error:
            raise CatalogSnapshotUnavailable("catalog changed during snapshot") from error
        if after != before:
            raise CatalogSnapshotUnavailable("catalog changed during snapshot")
        yield destination


def _catalog_file_state(path: Path) -> dict[str, dict[str, object]]:
    state: dict[str, dict[str, object]] = {}
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if candidate == path:
                raise
            continue
        if not stat.S_ISREG(info.st_mode) or candidate.is_symlink():
            raise ValueError("catalog path is unsafe")
        if info.st_size > MAX_CATALOG_FILE_BYTES:
            raise ValueError("catalog file exceeds snapshot size limit")
        digest = hashlib.sha256()
        with _pinned_directory(path.parent, path.parent) as parent_fd:
            with _open_pinned_regular(
                candidate, parent_fd=parent_fd, name=candidate.name
            ) as (descriptor, _):
                total = 0
                while chunk := os.read(descriptor, 65_536):
                    total += len(chunk)
                    if total > MAX_CATALOG_FILE_BYTES:
                        raise ValueError("catalog file exceeds snapshot size limit")
                    digest.update(chunk)
        current = candidate.lstat()
        if (
            current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns
        ) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
            raise ValueError("catalog file changed while hashing")
        state[candidate.name] = {
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "sha256": digest.hexdigest(),
        }
    return state


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
