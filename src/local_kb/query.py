"""Create bounded evidence packets for model-neutral local answering."""

from __future__ import annotations

from collections.abc import Callable, Collection
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import uuid4

from .catalog import Catalog
from .correction_index import CorrectionIndex
from .correction_match import (
    CorrectionMatcher,
    features_from_packet_inputs,
)
from .correction_store import CorrectionStore
from .paths import VaultPaths
from .queue import DiskQueue
from .search import EvidenceHit, MAX_RESULTS, _deduplicate, exact_routes, has_searchable_terms, ranked_search, validate_question, validate_spaces


SCHEMA_VERSION = 2
MAX_EVIDENCE_TEXT = 8_000
MAX_PACKET_BYTES = 384_000
MAX_PENDING_JOBS = 40
MAX_JOB_TEXT = 256
_FRONTMATTER_LINE = re.compile(r"^([a-z_]+):\s*(.*)$")


def evidence_sha256(item: dict[str, object]) -> str:
    """Hash the immutable evidence identity and exact text, not mutable ranking."""
    if not isinstance(item, dict):
        raise TypeError("evidence must be an object")
    kind = item.get("kind")
    if kind == "raw_fragment":
        fields = ("kind", "source_id", "version_id", "locator", "text")
    elif kind == "derived_wiki":
        fields = ("kind", "path", "locator", "source_ids", "text")
    else:
        raise ValueError("unsupported evidence kind")
    canonical = {field: item.get(field) for field in fields}
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_reparse(path: Path) -> bool:
    try:
        return bool(os.lstat(path).st_file_attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except AttributeError:
        return False
    except OSError:
        return True


def _safe_existing_tree(root: Path, target: Path) -> None:
    root = root.absolute()
    target = target.absolute()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("path must stay inside the vault") from error
    cursor = root
    for part in target.relative_to(root).parts:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            if cursor.is_symlink() or _is_reparse(cursor):
                raise ValueError("symlinked or reparse-point paths are not allowed")


def _safe_component(component: str) -> None:
    stem = component.rstrip(". ").split(".", 1)[0].upper()
    if (not component or component in {".", ".."} or component != component.rstrip(". ")
            or ":" in component or any(ord(character) < 32 or ord(character) == 127 for character in component)
            or stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem)):
        raise ValueError("packet output contains an unsafe path component")


def _safe_walk_wiki(root: Path, scan_state: dict[str, bool]):
    """Yield a small, sorted tree without following links, junctions, or reparse points."""
    pending: list[tuple[Path, int]] = [(root, 0)]
    scanned = 0
    while pending and scanned < 400:
        directory, depth = pending.pop()
        if depth > 8:
            scan_state["truncated"] = True
            continue
        try:
            # The yielded file remains beneath this pinned parent until the caller
            # resumes the generator, closing the parent-junction replacement race.
            with _pinned_directory(root, directory) as directory_fd:
                entries = []
                with os.scandir(directory_fd if directory_fd is not None else directory) as stream:
                    for entry in stream:
                        if len(entries) >= 400:
                            scan_state["truncated"] = True
                            break
                        entries.append(entry)
                entries.sort(key=lambda entry: entry.name.casefold())
                for entry in entries:
                    scanned += 1
                    if scanned > 400:
                        scan_state["truncated"] = True
                        return
                    path = directory / entry.name
                    info = entry.stat(follow_symlinks=False)
                    if entry.is_symlink() or _is_reparse(path):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        pending.append((path, depth + 1))
                    elif entry.name.casefold().endswith(".md") and stat.S_ISREG(info.st_mode):
                        yield path, directory_fd, entry.name
        except OSError:
            continue
    if pending and scanned >= 400:
        scan_state["truncated"] = True


@contextmanager
def _pinned_directory(root: Path, directory: Path):
    """Pin every directory component while enumerating or opening a wiki page."""
    from .compiler import ManualCompiler
    _safe_existing_tree(root, directory)
    locker = ManualCompiler(directory, trusted_root=root)
    with locker._pinned_outbox(create=False) as descriptor:
        yield descriptor


@contextmanager
def _open_pinned_regular(path: Path, *, parent_fd: int | None = None, name: str | None = None):
    """Open a regular snapshot and reject replacement, links, and special files."""
    before = (os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
              if parent_fd is not None and name is not None else os.lstat(path))
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or _is_reparse(path):
        raise ValueError("wiki page is not a safe regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = (os.open(name, flags, dir_fd=parent_fd) if parent_fd is not None and name is not None
                  else os.open(path, flags))
    try:
        opened = os.fstat(descriptor)
        token = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != token:
            raise ValueError("wiki page changed while opening")
        yield descriptor, token
        after = os.fstat(descriptor)
        current = (os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                   if parent_fd is not None and name is not None else os.lstat(path))
        if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != token
                or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != token):
            raise ValueError("wiki page changed while reading")
    finally:
        os.close(descriptor)


def _safe_read_wiki(catalog: Catalog, vault: VaultPaths, question: str, spaces: tuple[str, ...]) -> tuple[list[dict[str, object]], bool, list[str]]:
    """Read only bounded Current State text from a caller-provided vault."""
    wiki_root = vault.wiki.absolute()
    if not wiki_root.is_dir():
        return [], False, []
    _safe_existing_tree(vault.root, wiki_root)
    terms = [term.casefold() for term in Catalog._plain_query_terms(question) if len(term) > 1]
    if not terms:
        return [], False, []
    candidates: list[dict[str, object]] = []
    warnings: list[str] = []
    scan_state = {"truncated": False}
    # A deterministic cap prevents a huge wiki tree becoming an unbounded query operation.
    for path, parent_fd, name in _safe_walk_wiki(wiki_root, scan_state):
        try:
            # On POSIX, the generator still owns parent_fd here: this open cannot
            # resolve through a renamed parent or a replacement symlink/junction.
            with _open_pinned_regular(path, parent_fd=parent_fd, name=name) as (descriptor, token):
                if token[2] > 128_000:
                    continue
                chunks = bytearray()
                while chunk := os.read(descriptor, 65_536):
                    chunks.extend(chunk)
                    if len(chunks) > 128_000:
                        raise ValueError("wiki page exceeds read limit")
                text = bytes(chunks).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if len(text) > 128_000 or not text.startswith("---\n"):
            continue
        header, marker, body = text[4:].partition("\n---\n")
        if not marker or len(header) > 16_000:
            continue
        values = {match.group(1): _frontmatter_scalar(match.group(2)) for line in header.splitlines() if (match := _FRONTMATTER_LINE.match(line))}
        space = values.get("space")
        if space not in spaces:
            continue
        current = body.partition("## Current State\n")[2].partition("\n## ")[0].strip()
        aliases = _frontmatter_list(header, "aliases")
        title_and_aliases = " ".join([values.get("title", ""), *aliases]).casefold()
        direct_routes = tuple(route for route in _significant_routes(question) if route.casefold() in title_and_aliases or route.casefold() in current.casefold())[:16]
        if not current:
            continue
        source_ids = _frontmatter_list(header, "source_ids")
        candidate = {
            "kind": "derived_wiki", "evidence_class": "derived", "space": space, "path": path.relative_to(vault.root).as_posix(),
            "locator": "Current State", "text": current[:MAX_EVIDENCE_TEXT],
            "source_ids": source_ids, "score": float(len(direct_routes) + sum(route.casefold() in title_and_aliases for route in direct_routes)),
            "route": "direct_wiki", "routes": list(direct_routes), "coverage": len(direct_routes), "match_kind": "direct",
            "page_id": values.get("id", ""), "title": values.get("title", ""), "aliases": aliases,
            "related": _frontmatter_list(header, "related"), "page_type": values.get("type", ""),
            "updated_at": values.get("updated_at", ""),
            "truncated": len(current) > MAX_EVIDENCE_TEXT,
        }
        candidate["evidence_sha256"] = evidence_sha256(candidate)
        candidates.append(candidate)
    all_ids = list(dict.fromkeys(source_id for item in candidates for source_id in item["source_ids"]))[:128]
    paths_by_source: dict[str, list[str]] = {}
    if all_ids:
        marks = ", ".join("?" for _ in all_ids)
        with catalog.connection() as connection:
            for row in connection.execute(f"SELECT source_id, space, relative_path FROM sources WHERE source_id IN ({marks})", all_ids):
                paths_by_source.setdefault(row["source_id"], []).append((row["space"], row["relative_path"]))
    findings: list[dict[str, object]] = []
    for item in candidates:
        if not _valid_wiki_provenance_paths(vault, item["source_ids"], paths_by_source, str(item["space"])):
            warnings.append("wiki_page_skipped_invalid_provenance")
            continue
        findings.append(item)
    direct = [item for item in findings if item["coverage"]]
    date_terms = tuple(route for route in _significant_routes(question) if re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", route))
    decision = "決策" in question or "decision" in question.casefold()
    for item in direct:
        if decision and item["page_type"] == "decision": item["score"] = float(item["score"]) + 2
        if date_terms and any(term in str(item["updated_at"]) or term in str(item["text"]) for term in date_terms): item["score"] = float(item["score"]) + 1
    direct.sort(key=lambda item: (-float(item["score"]), str(item["path"])))
    expanded: list[dict[str, object]] = []
    if direct:
        direct_keys = {str(item["page_id"]) for item in direct} | {str(item["path"]) for item in direct} | {str(item["title"]) for item in direct}
        for item in findings:
            if item in direct or item["space"] != direct[0]["space"]: continue
            common = any(set(item["source_ids"]) & set(parent["source_ids"]) for parent in direct)
            related = any(target in direct_keys for target in item["related"]) or any(str(item["path"]) in parent["related"] for parent in direct)
            if not (common or related): continue
            item["match_kind"] = "expanded"; item["route"] = "expanded_related" if related else "expanded_common_source"; item["score"] = -1.0 - len(expanded)/1000
            expanded.append(item)
            if len(expanded) >= 4: break
    public = direct + expanded
    for item in public:
        for key in ("page_id", "title", "aliases", "related", "page_type", "updated_at"): item.pop(key, None)
    invalid_count = len(warnings)
    warning_summary = (["wiki_page_skipped_invalid_provenance"] if invalid_count else [])
    if invalid_count:
        warning_summary.append(f"wiki_invalid_provenance_count:{invalid_count}")
    return public[:MAX_RESULTS], scan_state["truncated"], warning_summary


def _valid_wiki_provenance_paths(vault: VaultPaths, source_ids: list[str], paths_by_source: dict[str, list[tuple[str, str]]], space: str) -> bool:
    if not source_ids or any(source_id not in paths_by_source for source_id in source_ids):
        return False
    for source_id in source_ids:
        readable = False
        for row_space, relative in paths_by_source[source_id]:
            if row_space != space:
                continue
            expected = f"10_raw/{space}/{source_id}/"
            if not str(relative).replace("\\", "/").startswith(expected):
                continue
            candidate = vault.root / relative
            try:
                with _pinned_directory(vault.raw, candidate.parent) as parent_fd:
                    with _open_pinned_regular(candidate, parent_fd=parent_fd, name=candidate.name) as (descriptor, _):
                        os.read(descriptor, 1)
                        readable = True
                        break
            except (OSError, ValueError):
                continue
        if not readable:
            return False
    return True


def _frontmatter_list(header: str, name: str) -> list[str]:
    lines = header.splitlines()
    values: list[str] = []
    collecting = False
    for line in lines:
        if line == f"{name}:":
            collecting = True
            continue
        if collecting and line.startswith("  - "):
            values.append(line[4:].strip().strip('"'))
            continue
        if collecting:
            break
    return values[:32]


def _frontmatter_scalar(value: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value.strip()
    return parsed if isinstance(parsed, str) else ""


def _significant_routes(question: str) -> tuple[str, ...]:
    from .search import significant_routes
    return significant_routes(question)


def _raw_evidence(hits: list[EvidenceHit]) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    for hit in hits:
        item = {
            "kind": "raw_fragment", "source_id": hit.source_id, "version_id": hit.version_id,
            "space": hit.space, "path": hit.relative_path, "locator": hit.locator,
            "text": hit.text[:MAX_EVIDENCE_TEXT], "score": round(hit.score, 6),
            "route": hit.route,
            "routes": list(hit.routes), "coverage": hit.coverage,
            "truncated": len(hit.text) > MAX_EVIDENCE_TEXT,
        }
        item["evidence_sha256"] = evidence_sha256(item)
        evidence.append(item)
    return evidence


def _short(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.replace("\x00", "").replace("\r", " ").replace("\n", " ")[:MAX_JOB_TEXT]


def _pending_jobs(queue: DiskQueue | None, spaces: tuple[str, ...], question: str) -> dict[str, object]:
    if queue is None:
        return {"scope": "selected_spaces", "jobs": [], "total": 0, "shown": 0, "truncated": False, "related_total": 0, "related_shown": 0, "unknown_total": 0, "unknown_shown": 0, "queue_scan_truncated": False, "counts_are_lower_bound": False}
    jobs: list[dict[str, object]] = []
    unknown_space = False
    try:
        candidates, queue_scan_truncated = queue.iter_jobs_bounded(1000)
    except (OSError, ValueError, AttributeError):
        return {"scope": "all-active", "jobs": [], "total": 0, "shown": 0, "truncated": True, "related_total": 0, "related_shown": 0, "unknown_total": 0, "unknown_shown": 0, "queue_scan_truncated": True, "counts_are_lower_bound": True, "error": "queue_unavailable"}
    for job in candidates:
        source_metadata = job.metadata.get("source")
        source_values = source_metadata if isinstance(source_metadata, dict) else {}
        source_status = _short(source_values.get("status"))
        if job.state == "published" and source_status != "pending_extractor":
            continue
        space = _short(job.metadata.get("space"))
        if space is None:
            space = _short(source_values.get("space"))
        if space is not None and space not in spaces:
            continue
        if space is None:
            unknown_space = True
        compiler_status = _short(job.metadata.get("compiler_status"))
        pending_reason = (
            "extractor_required" if source_status == "pending_extractor"
            else "needs_agent" if compiler_status == "needs_agent"
            else "job_error" if job.error else "incomplete_job"
        )
        metadata = " ".join(filter(None, [
            _short(job.metadata.get("source_id")), _short(source_values.get("source_id")),
            _short(source_values.get("original_name")), _short(job.source_path), _short(job.error),
        ])).casefold()
        relation = "matched_metadata" if any(route.casefold() in metadata for route in _significant_routes(question)) else "unknown"
        jobs.append({
            "job_id": job.job_id, "state": job.state,
            "compiler_status": compiler_status, "source_status": source_status,
            "pending_reason": pending_reason,
            "relation": relation,
            "source": (_short(job.metadata.get("source_id")) or _short(source_values.get("source_id"))
                       or _short(source_values.get("original_name")) or _short(job.source_path)),
            "space": space, "error": _short(job.error),
        })
    jobs.sort(key=lambda item: (item["relation"] != "matched_metadata", str(item["job_id"])))
    shown = jobs[:MAX_PENDING_JOBS]
    related = [item for item in jobs if item["relation"] == "matched_metadata"]
    unknown = [item for item in jobs if item["relation"] != "matched_metadata"]
    return {"scope": "selected_spaces_plus_unknown" if unknown_space else "selected_spaces", "jobs": shown, "total": len(jobs), "shown": len(shown), "truncated": len(jobs) > MAX_PENDING_JOBS or queue_scan_truncated,
            "related_total": len(related), "related_shown": sum(item["relation"] == "matched_metadata" for item in shown),
            "unknown_total": len(unknown), "unknown_shown": sum(item["relation"] != "matched_metadata" for item in shown),
            "queue_scan_truncated": queue_scan_truncated, "counts_are_lower_bound": queue_scan_truncated}


def _empty_correction_context(
    *,
    index_available: bool,
    save_allowed: bool,
    warnings: list[str] | None = None,
    truncated: bool = False,
) -> dict[str, object]:
    return {
        "applicable": [],
        "possible": [],
        "scan": {
            "total_considered": 0,
            "applicable_count": 0,
            "possible_count": 0,
            "truncated": truncated,
            "index_available": index_available,
            "save_allowed": save_allowed,
        },
        "warnings": list(warnings or []),
    }


def _correction_context(
    catalog: Catalog,
    vault: VaultPaths | None,
    question: str,
    spaces: tuple[str, ...],
    evidence: list[dict[str, object]],
) -> dict[str, object]:
    if vault is None:
        return _empty_correction_context(
            index_available=False,
            save_allowed=False,
            warnings=["correction_unavailable"],
        )
    try:
        store = CorrectionStore(vault)
        records, records_truncated = store.iter_records()
    except (OSError, ValueError):
        return _empty_correction_context(
            index_available=False,
            save_allowed=False,
            warnings=["correction_unavailable"],
        )
    index = CorrectionIndex(vault)
    if not records:
        if vault.correction_index.exists() and not index.integrity_check():
            return _empty_correction_context(
                index_available=False,
                save_allowed=False,
                warnings=["correction_unavailable"],
            )
        return _empty_correction_context(
            index_available=True,
            save_allowed=not records_truncated,
            warnings=(
                ["correction_scan_truncated"]
                if records_truncated
                else []
            ),
            truncated=records_truncated,
        )
    if not vault.correction_index.is_file() or not index.integrity_check():
        return _empty_correction_context(
            index_available=False,
            save_allowed=False,
            warnings=["correction_unavailable"],
        )
    by_id = {record.correction_id: record for record in records}
    routes = tuple(_significant_routes(question))[:64]
    applicable = []
    possible = []
    considered = 0
    truncated = records_truncated
    try:
        for space in spaces:
            candidate_ids, candidate_truncated = index.candidates(
                space=space,
                terms=routes,
                limit=100,
            )
            if not candidate_ids and routes:
                candidate_ids, fallback_truncated = index.candidates(
                    space=space,
                    terms=(),
                    limit=100,
                )
                candidate_truncated = (
                    candidate_truncated or fallback_truncated
                )
            truncated = truncated or candidate_truncated
            candidates = [
                by_id[correction_id]
                for correction_id in candidate_ids
                if correction_id in by_id
            ]
            features = features_from_packet_inputs(
                catalog,
                question,
                space,
                [
                    item
                    for item in evidence
                    if item.get("space") == space
                ],
            )
            result = CorrectionMatcher(candidates).match(features)
            applicable.extend(result.applicable)
            possible.extend(result.possible)
            considered += result.total_considered
            truncated = truncated or result.truncated
    except (OSError, ValueError, sqlite3.Error):
        return _empty_correction_context(
            index_available=False,
            save_allowed=False,
            warnings=["correction_unavailable"],
        )
    applicable = applicable[: CorrectionMatcher.MAX_APPLICABLE]
    possible = possible[: CorrectionMatcher.MAX_POSSIBLE]
    warnings = ["correction_scan_truncated"] if truncated else []
    return {
        "applicable": applicable,
        "possible": possible,
        "scan": {
            "total_considered": considered,
            "applicable_count": len(applicable),
            "possible_count": len(possible),
            "truncated": truncated,
            "index_available": True,
            "save_allowed": not truncated,
        },
        "warnings": warnings,
    }


class QueryService:
    def __init__(
        self, catalog: Catalog, *, vault: VaultPaths | None = None,
        queue: DiskQueue | None = None, clock: Callable[[], str] = _now,
    ) -> None:
        self.catalog = catalog
        self.vault = vault
        self.queue = queue
        self.clock = clock

    def prepare(self, question: str, spaces: Collection[str], *, limit: int = MAX_RESULTS, space_selection: str = "explicit") -> dict[str, object]:
        checked_question = validate_question(question)
        checked_spaces = validate_spaces(spaces)
        catalog_available = self.catalog.path.is_file()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
            raise ValueError(f"limit must be between 1 and {MAX_RESULTS}")
        if not catalog_available:
            raw = []
            reason = "catalog_unavailable"
        elif not has_searchable_terms(checked_question):
            raw: list[EvidenceHit] = []
            reason = "question_has_no_searchable_terms"
        else:
            raw = _deduplicate(
                ranked_search(self.catalog, checked_question, checked_spaces, limit=limit)
                + exact_routes(self.catalog, checked_question, checked_spaces, limit=limit), limit,
            )
            reason = "no_matching_evidence" if not raw else None
        evidence = _raw_evidence(raw)
        wiki_scan_truncated = False
        # Generated wiki pages are useful secondary context only.  Raw extraction is
        # always listed first and is never relabelled as original evidence.
        if self.vault is not None and catalog_available and has_searchable_terms(checked_question):
            derived, wiki_scan_truncated, warnings = _safe_read_wiki(self.catalog, self.vault, checked_question, checked_spaces)
            wiki_quota = min(5, limit - 1)
            selected_derived = derived[:wiki_quota] if limit >= 2 else []
            raw_recalled = evidence
            raw_slots = limit - len(selected_derived)
            evidence = raw_recalled[:raw_slots] + selected_derived
        else:
            warnings = []
            evidence = evidence[:limit]
        if evidence:
            reason = None
        elif not catalog_available:
            reason = "catalog_unavailable"
        elif not has_searchable_terms(checked_question):
            reason = "question_has_no_searchable_terms"
        else:
            reason = "no_matching_evidence"
        pending_jobs = _pending_jobs(self.queue, checked_spaces, checked_question)
        correction_context = _correction_context(
            self.catalog,
            self.vault,
            checked_question,
            checked_spaces,
            evidence,
        )
        packet: dict[str, object] = {
            "schema_version": SCHEMA_VERSION, "question": checked_question,
            "prepared_at": _validate_prepared_at(self.clock()), "spaces": list(checked_spaces),
            "space_selection": space_selection,
            "status": "ready" if evidence else "insufficient_evidence",
            "instructions": [
                "只能依據 evidence 回答，不可補造事實。",
                "raw_fragment 要引用 source_id、version_id、locator、evidence_sha256；derived_wiki 要引用 path、locator、source_ids、evidence_sha256，且不可當成原文。",
                "遇到衝突、未知或證據不足時，明確說明並降低信心。",
                "不要把 pending_jobs 當成已完成或已驗證的證據。",
                "逐項處理 applicable_corrections；每筆必須回報 applied、not_applicable 或 conflict，不得用修正取代原始證據。",
            ],
            "evidence": evidence, "pending_jobs": pending_jobs,
            "warnings": warnings,
            "applicable_corrections": correction_context["applicable"],
            "possible_corrections": correction_context["possible"],
            "correction_scan": correction_context["scan"],
            "correction_warnings": correction_context["warnings"],
            "truncated": {
                "evidence": any(item.get("truncated") is True for item in evidence),
                "pending_jobs": pending_jobs["truncated"],
                "result_limit_reached": len(evidence) >= limit,
                "raw_results": 'raw_recalled' in locals() and len(raw_recalled) > sum(item.get("kind") == "raw_fragment" for item in evidence),
                "wiki_scan": wiki_scan_truncated,
            },
        }
        if reason is not None:
            packet["reason"] = reason
        encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_PACKET_BYTES:
            # The per-fragment cap normally prevents this; fail closed rather than silently
            # presenting an incomplete packet as complete.
            raise ValueError("prepared packet exceeds size limit")
        return packet


def write_packet(vault: VaultPaths, packet: dict[str, object], output: Path | str | None = None) -> Path:
    """Atomically replace a regular packet only under ``.kb`` or ``30_answers``."""
    if not isinstance(packet, dict):
        raise TypeError("packet must be an object")
    try:
        payload = json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    except (TypeError, ValueError) as error:
        raise ValueError("packet must be safely JSON serializable") from error
    if len(payload.encode("utf-8")) > MAX_PACKET_BYTES:
        raise ValueError("packet exceeds size limit")
    root = vault.root.absolute()
    requested = vault.runtime / "last-packet.json" if output is None else Path(output)
    destination = (root / requested).absolute() if not requested.is_absolute() else requested.absolute()
    for component in destination.relative_to(root).parts:
        _safe_component(component)
    _safe_existing_tree(root, destination.parent)
    allowed = (vault.runtime.absolute(), vault.answers.absolute())
    if not any(destination.is_relative_to(directory) for directory in allowed):
        raise ValueError("packet output must be inside .kb or 30_answers")
    if destination.suffix.lower() != ".json" or destination.name in {".", ".."}:
        raise ValueError("packet output must be a JSON file")
    # This must already be a trusted vault location; do not create a user supplied
    # nested output directory through a path that could be swapped for a junction.
    if not destination.parent.is_dir():
        raise ValueError("packet output parent must already exist")
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or _is_reparse(destination) or not destination.is_file():
            raise ValueError("packet output must be a regular file")
    from .compiler import ManualCompiler
    locker = ManualCompiler(destination.parent, trusted_root=root)
    with locker._pinned_outbox() as directory_fd:
        temporary_name = f".{destination.name}.{uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            descriptor = (os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
                          if directory_fd is not None else os.open(destination.parent / temporary_name, flags, 0o600))
            encoded = payload.encode("utf-8")
            offset = 0
            while offset < len(encoded):
                offset += os.write(descriptor, encoded[offset:])
            os.fsync(descriptor)
            os.close(descriptor); descriptor = None
            if directory_fd is not None:
                os.replace(temporary_name, destination.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                os.fsync(directory_fd)
            else:
                os.replace(destination.parent / temporary_name, destination)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                if directory_fd is not None:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                else:
                    (destination.parent / temporary_name).unlink(missing_ok=True)
            except FileNotFoundError:
                pass
    return destination.absolute()


def _validate_prepared_at(value: object) -> str:
    if not isinstance(value, str) or len(value) > 64 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("prepared_at must be a safe UTC ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("prepared_at must be a UTC ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("prepared_at must be a UTC ISO-8601 timestamp")
    return value
