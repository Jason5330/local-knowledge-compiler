"""Turn one durable inbox job into raw evidence, catalog rows and cache data."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any
from uuid import uuid4

from .catalog import Catalog
from .compiler import (
    MAX_CHANGES, MAX_EVIDENCE_CHARS, ManualCompiler, _run_bounded_process,
)
from .extractors import registry as default_registry
from .models import Job, SourceVersion
from .paths import VaultPaths
from .queue import DiskQueue
from .source_store import SourceStore
from .transaction import ChangeTransaction
from .wiki import WikiPage, render_page


_COMPILER_FIELDS = frozenset({
    "path", "title", "type", "space", "confidence", "source_ids",
    "current_state", "conflicts", "timeline_entry",
})
_WIKI_TYPES = frozenset({"concept", "entity", "topic", "decision", "timeline", "project"})
_WIKI_SPACES = frozenset({"personal", "work", "shared", "unclassified"})
_WIKI_CONFIDENCES = frozenset({"high", "medium", "low"})
_PROJECT_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_WINDOWS_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
    "LPT6", "LPT7", "LPT8", "LPT9",
})
_RECEIPT_SCHEMA_VERSION = 1
_MAX_RECEIPT_BYTES = 2_000_000
_MAX_GIT_METADATA_BYTES = 4_096
_GIT_TIMEOUT_SECONDS = 30.0
_DERIVED_HEADER_BYTES = 8_192
_DERIVED_TYPE = re.compile(r'(?m)^type:\s*(?:"derived-answer"|derived-answer)\s*$')


def _safe_wiki_path(value: object) -> str:
    """Accept only one canonical Markdown path below the managed wiki root."""
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ValueError("compiler path must be a bounded non-empty string")
    if ("\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value)
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError("compiler path is not a safe POSIX relative path")
    path = PurePosixPath(value)
    if (path.as_posix() != value or path.is_absolute() or len(path.parts) < 2
            or path.parts[0] != "20_wiki" or any(part in {"", ".", ".."} for part in path.parts)
            or path.suffix != ".md"):
        raise ValueError(f"compiler path outside wiki: {value}")
    for part in path.parts:
        stem = part.rstrip(". ").split(".", 1)[0].upper()
        if part != part.rstrip(". ") or ":" in part or stem in _WINDOWS_RESERVED:
            raise ValueError("compiler path uses an unsafe Windows name")
    return path.as_posix()


def _safe_compiler_text(value: object, field: str, *, empty: bool = False, limit: int = 50_000) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f"compiler {field} must be bounded non-empty text")
    if any(ord(character) == 127 or (ord(character) < 32 and character not in {"\n", "\t"}) for character in value):
        raise ValueError(f"compiler {field} contains a control character")
    return value


def _safe_compiler_line(value: object, field: str, *, limit: int = 300) -> str:
    text = _safe_compiler_text(value, field, limit=limit)
    if "\n" in text or "\t" in text:
        raise ValueError(f"compiler {field} must be a single line")
    return text


@dataclass(frozen=True)
class _CompileOutcome:
    paths: tuple[str, ...] = ()
    handoff: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class IngestService:
    def __init__(
        self,
        vault: VaultPaths | Path | str,
        queue: DiskQueue,
        catalog: Catalog,
        compiler: Any = None,
        *,
        registry: Any = None,
    ) -> None:
        self.vault = vault if isinstance(vault, VaultPaths) else VaultPaths(Path(vault).resolve())
        self.queue = queue
        self.catalog = catalog
        self.registry = registry or default_registry
        self.store = SourceStore(self.vault.raw)
        self.compiler = compiler or ManualCompiler(
            self.vault.runtime / "manual", trusted_root=self.vault.root,
        )

    def process(self, job_id: str, *, space: str = "unclassified") -> SourceVersion:
        """Process one job; persist each recoverable boundary before advancing."""
        job = self.queue.get(job_id)
        try:
            self._reject_derived_input(job)
            self.catalog.initialize()
            if "compilation_receipt" in job.metadata:
                final, receipt = self._receipt_resume_inputs(job)
                self._apply_compilation_receipt(receipt, final)
                self._complete_compilation(job_id, final)
                return final
            if job.state == "pending_attention" and job.metadata.get("compiler_status") == "needs_agent":
                # A manual handoff is deliberately terminal for normal ingest.
                # Only resume_compilation() may request another model attempt.
                self._validate_pending_compiler_metadata(job)
                source = self._source_for(job)
                extraction = job.metadata.get("extraction")
                if source is None or not isinstance(extraction, dict) or not self._valid_extraction(extraction):
                    raise ValueError("pending compiler job is incomplete")
                return replace(source, status=extraction["status"])
            job = self._claim(job)
            source = self._source_for(job)
            if source is None:
                source = self._archive(job, space)
                job = self.queue.get(job_id)
            extraction = self._extraction_for(job, source)
            final = replace(source, status=extraction["status"])
            fragments = [(item["locator"], item["text"]) for item in extraction["fragments"]]
            self._write_cache(final, extraction)
            processed = self._move_processed(self.queue.get(job_id), final)
            self._mark(job_id, "validated", source=asdict(final), processed_path=str(processed.relative_to(self.vault.root)))
            self.catalog.upsert_source(final, fragments)
            self._cleanup_claim_staging(job_id)
            outcome = _CompileOutcome()
            if final.status == "extracted":
                outcome = self._prepare_compilation(final, extraction)
                if outcome.handoff is not None:
                    self._mark_compiler_pending(
                        job_id,
                        final,
                        processed.relative_to(self.vault.root).as_posix(),
                        outcome.handoff,
                    )
            if outcome.handoff is not None:
                return final
            if outcome.receipt is not None:
                self._persist_compilation_receipt(job_id, final, outcome.receipt)
                self._apply_compilation_receipt(outcome.receipt, final)
                self._complete_compilation(job_id, final)
                return final
            self._mark(job_id, "published", source=asdict(final), processed_path=str(processed.relative_to(self.vault.root)))
            return final
        except BaseException as error:
            self.queue.fail(job_id, error)
            raise

    def _reject_derived_input(self, job: Job) -> None:
        """Keep generated answers out of the immutable raw evidence pipeline."""
        candidates: list[Path] = []
        for value in (
            job.source_path,
            job.metadata.get("original_source_path"),
            job.metadata.get("claimed_path"),
        ):
            if isinstance(value, str) and value:
                candidate = Path(os.path.abspath(value))
                if candidate not in candidates:
                    candidates.append(candidate)
        answers = Path(os.path.abspath(os.fspath(self.vault.answers)))
        for candidate in candidates:
            if candidate.is_relative_to(answers):
                raise ValueError("derived answer cannot be ingested as a raw source")
            if candidate.suffix.casefold() not in {".md", ".markdown"}:
                continue
            try:
                with self._open_pinned_regular(candidate) as (descriptor, _):
                    data = os.read(descriptor, _DERIVED_HEADER_BYTES + 1)
            except FileNotFoundError:
                continue
            if len(data) > _DERIVED_HEADER_BYTES or not data.startswith(b"---\n"):
                continue
            marker = data.find(b"\n---\n", 4)
            if marker < 0:
                continue
            try:
                header = data[4:marker].decode("utf-8")
            except UnicodeDecodeError:
                continue
            if _DERIVED_TYPE.search(header):
                raise ValueError("derived answer cannot be ingested as a raw source")

    def compile_extraction(self, source: SourceVersion, extraction: dict[str, Any]) -> list[str]:
        """Public compatibility wrapper: return pages, never a manual handoff path."""
        outcome = self._prepare_compilation(source, extraction)
        if outcome.receipt is not None:
            self._apply_compilation_receipt(outcome.receipt, source)
        return list(outcome.paths)

    def _prepare_compilation(
        self, source: SourceVersion, extraction: dict[str, Any], *, compiler: Any = None
    ) -> _CompileOutcome:
        """Compile and render an immutable receipt without touching live Wiki files."""
        if source.status != "extracted":
            return _CompileOutcome()
        evidence = self._compiler_evidence(source, extraction)
        result = (compiler or self.compiler).compile(evidence)
        if isinstance(result, Path):
            return _CompileOutcome(handoff=self._handoff_metadata(result))
        changes = self._validated_compiler_changes(result, source)
        now = _utc_now()
        pages: list[tuple[str, WikiPage]] = []
        for relative, change in changes:
            pages.append((relative, WikiPage(
                page_id="page_" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16],
                title=change["title"],
                page_type=change["type"],
                space=change["space"],
                confidence=change["confidence"],
                source_ids=tuple(change["source_ids"]),
                current_state=change["current_state"],
                conflicts=change["conflicts"],
                timeline_entry=change["timeline_entry"],
                updated_at=now,
            )))
        # Rendering invokes shared Wiki validation before the receipt is persisted.
        rendered = [(relative, render_page(page)) for relative, page in pages]
        receipt = {
            "schema_version": _RECEIPT_SCHEMA_VERSION,
            "source_id": source.source_id,
            "version_id": source.version_id,
            "updated_at": now,
            "pages": [
                {
                    "path": relative,
                    "content": content,
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                }
                for relative, content in rendered
            ],
        }
        self._receipt_bytes(receipt)
        return _CompileOutcome(
            paths=tuple(relative for relative, _ in rendered), receipt=receipt
        )

    def resume_compilation(self, job_id: str, *, compiler: Any = None) -> SourceVersion:
        """Recompile one durable manual handoff without re-ingesting its raw file."""
        job = self.queue.get(job_id)
        if "compilation_receipt" in job.metadata:
            final, receipt = self._receipt_resume_inputs(job)
            self._apply_compilation_receipt(receipt, final)
            self._complete_compilation(job_id, final)
            return final
        self._validate_pending_compiler_metadata(job)
        source = self._source_for(job)
        if source is None:
            raise ValueError("pending compiler job is missing its source")
        extraction = job.metadata.get("extraction")
        if not isinstance(extraction, dict) or not self._valid_extraction(extraction):
            raise ValueError("pending compiler job has invalid extraction metadata")
        final = replace(source, status=extraction["status"])
        if final.status != "extracted":
            raise ValueError("pending compiler job has no extracted evidence")
        outcome = self._prepare_compilation(final, extraction, compiler=compiler)
        if outcome.handoff is not None:
            processed_path = self._safe_processed_metadata(job.metadata.get("processed_path"))
            self._mark_compiler_pending(job_id, final, processed_path, outcome.handoff)
            return final
        assert outcome.receipt is not None
        self._persist_compilation_receipt(job_id, final, outcome.receipt)
        self._apply_compilation_receipt(outcome.receipt, final)
        self._complete_compilation(job_id, final)
        return final

    def _receipt_bytes(self, receipt: object) -> bytes:
        try:
            encoded = json.dumps(
                receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as error:
            raise ValueError("compilation receipt is not canonical JSON") from error
        if len(encoded) > _MAX_RECEIPT_BYTES:
            raise ValueError("compilation receipt exceeds size budget")
        return encoded

    def _validate_compilation_receipt(
        self, receipt: object, expected_hash: object, source: SourceVersion
    ) -> dict[str, Any]:
        if not isinstance(receipt, dict) or set(receipt) != {
            "schema_version", "source_id", "version_id", "updated_at", "pages",
        }:
            raise ValueError("compilation receipt schema is invalid")
        encoded = self._receipt_bytes(receipt)
        actual_hash = hashlib.sha256(encoded).hexdigest()
        if (not isinstance(expected_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
                or actual_hash != expected_hash):
            raise ValueError("compilation receipt hash is invalid")
        if (receipt["schema_version"] != _RECEIPT_SCHEMA_VERSION
                or isinstance(receipt["schema_version"], bool)
                or receipt["source_id"] != source.source_id
                or receipt["version_id"] != source.version_id):
            raise ValueError("compilation receipt source or version is invalid")
        updated_at = receipt["updated_at"]
        try:
            if not isinstance(updated_at, str):
                raise ValueError
            datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("compilation receipt timestamp is invalid") from error
        pages = receipt["pages"]
        if not isinstance(pages, list) or len(pages) > MAX_CHANGES:
            raise ValueError("compilation receipt pages are invalid")
        seen: set[str] = set()
        for page in pages:
            if not isinstance(page, dict) or set(page) != {"path", "content", "sha256"}:
                raise ValueError("compilation receipt page schema is invalid")
            relative = _safe_wiki_path(page["path"])
            if relative.casefold() in seen:
                raise ValueError("compilation receipt has duplicate paths")
            seen.add(relative.casefold())
            content = page["content"]
            digest = page["sha256"]
            if (not isinstance(content, str) or not isinstance(digest, str)
                    or hashlib.sha256(content.encode("utf-8")).hexdigest() != digest):
                raise ValueError("compilation receipt page hash is invalid")
        return receipt

    def _persist_compilation_receipt(
        self, job_id: str, source: SourceVersion, receipt: dict[str, Any]
    ) -> None:
        digest = hashlib.sha256(self._receipt_bytes(receipt)).hexdigest()
        self._validate_compilation_receipt(receipt, digest, source)

        def persist(current: Job) -> None:
            existing = current.metadata.get("compilation_receipt")
            if existing is not None:
                prior = self._validate_compilation_receipt(
                    existing, current.metadata.get("compilation_receipt_sha256"), source
                )
                if self._receipt_bytes(prior) != self._receipt_bytes(receipt):
                    raise ValueError("a different compilation receipt is already durable")
            current.state = "compiled"
            current.error = None
            current.metadata["source"] = asdict(source)
            current.metadata["compiler_status"] = "ready"
            current.metadata["compilation_receipt"] = receipt
            current.metadata["compilation_receipt_sha256"] = digest
            current.metadata["compiler_prepared_at"] = receipt["updated_at"]

        self.queue.update(job_id, persist)

    def _receipt_resume_inputs(
        self, job: Job
    ) -> tuple[SourceVersion, dict[str, Any]]:
        source = self._source_for(job)
        extraction = job.metadata.get("extraction")
        if source is None or not isinstance(extraction, dict) or not self._valid_extraction(extraction):
            raise ValueError("compilation receipt job is incomplete")
        final = replace(source, status=extraction["status"])
        if final.status != "extracted":
            raise ValueError("compilation receipt has no extracted source")
        if job.metadata.get("compiler_status") not in {"ready", "completed"}:
            raise ValueError("compilation receipt status is invalid")
        self._safe_processed_metadata(job.metadata.get("processed_path"))
        receipt = self._validate_compilation_receipt(
            job.metadata.get("compilation_receipt"),
            job.metadata.get("compilation_receipt_sha256"),
            final,
        )
        return final, receipt

    def _apply_compilation_receipt(
        self, receipt: dict[str, Any], source: SourceVersion
    ) -> list[str]:
        digest = hashlib.sha256(self._receipt_bytes(receipt)).hexdigest()
        validated = self._validate_compilation_receipt(receipt, digest, source)
        paths: list[str] = []
        changed: list[dict[str, str]] = []
        for page in validated["pages"]:
            relative = page["path"]
            paths.append(relative)
            target = self.vault.root / Path(*PurePosixPath(relative).parts)
            if os.path.lexists(target):
                try:
                    self._safe_regular_under(self.vault.wiki, target)
                    if self._hash_pinned_regular(target) == page["sha256"]:
                        continue
                except (OSError, ValueError) as error:
                    raise ValueError("compilation receipt live target is unsafe") from error
            changed.append(page)
        if validated["pages"]:
            transaction = ChangeTransaction(self.vault.root)
            for page in changed:
                transaction.stage(page["path"], page["content"])
            transaction.publish(lambda _: None)
            transaction.commit_git(f"kb: compile {source.version_id}", paths=paths)
            self._verify_receipt_git_head(validated)
        return paths

    def _verify_receipt_git_head(self, receipt: dict[str, Any]) -> None:
        """Require every receipt page to be an exact tracked blob in Git HEAD."""
        try:
            if not (self.vault.root / ".git").is_dir():
                raise RuntimeError("Git HEAD does not exist")
            top = self._bounded_git(
                ["git", "rev-parse", "--show-toplevel"],
                max_output_bytes=_MAX_GIT_METADATA_BYTES,
            ).decode("utf-8", errors="strict").strip()
            if Path(top).resolve() != self.vault.root.resolve():
                raise RuntimeError("Git HEAD belongs to a different repository")
            head = self._git_object_id("HEAD^{commit}")
            for page in receipt["pages"]:
                relative = page["path"]
                object_id = self._git_object_id(f"{head}:{relative}")
                object_type = self._bounded_git(
                    ["git", "cat-file", "-t", object_id],
                    max_output_bytes=_MAX_GIT_METADATA_BYTES,
                ).strip()
                if object_type != b"blob":
                    raise RuntimeError(f"Git HEAD receipt path is not a blob: {relative}")
                expected = page["content"].encode("utf-8")
                raw_size = self._bounded_git(
                    ["git", "cat-file", "-s", object_id],
                    max_output_bytes=_MAX_GIT_METADATA_BYTES,
                ).strip()
                if re.fullmatch(rb"[0-9]{1,20}", raw_size) is None:
                    raise RuntimeError("Git HEAD blob size is invalid")
                if int(raw_size) != len(expected):
                    raise RuntimeError(
                        f"Git HEAD blob size does not match receipt page: {relative}"
                    )
                committed = self._bounded_git(
                    ["git", "cat-file", "blob", object_id],
                    max_output_bytes=max(1, len(expected)),
                )
                if (hashlib.sha256(committed).hexdigest() != page["sha256"]
                        or committed != expected):
                    raise RuntimeError(
                        f"Git HEAD does not exactly contain receipt page: {relative}"
                    )
            if self._git_object_id("HEAD^{commit}") != head:
                raise RuntimeError("Git HEAD changed during compilation receipt verification")
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise RuntimeError(f"Git HEAD cannot verify compilation receipt: {error}") from error

    def _bounded_git(self, command: list[str], *, max_output_bytes: int) -> bytes:
        returncode, stdout, overflowed, timed_out = _run_bounded_process(
            command, None, cwd=self.vault.root, timeout=_GIT_TIMEOUT_SECONDS,
            max_output_bytes=max_output_bytes,
        )
        if timed_out:
            raise RuntimeError("Git HEAD verification timed out")
        if overflowed:
            raise RuntimeError("Git HEAD verification output exceeded its bound")
        if returncode != 0:
            raise RuntimeError("Git HEAD verification command failed")
        return stdout

    def _git_object_id(self, revision: str) -> str:
        output = self._bounded_git(
            ["git", "rev-parse", "--verify", revision],
            max_output_bytes=_MAX_GIT_METADATA_BYTES,
        ).strip()
        if re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", output) is None:
            raise RuntimeError("Git HEAD object id is invalid")
        return output.decode("ascii")

    def _complete_compilation(self, job_id: str, source: SourceVersion) -> None:
        def complete(current: Job) -> None:
            self._validate_compilation_receipt(
                current.metadata.get("compilation_receipt"),
                current.metadata.get("compilation_receipt_sha256"),
                source,
            )
            current.state = "published"
            current.error = None
            current.metadata["source"] = asdict(source)
            current.metadata["compiler_status"] = "completed"
            current.metadata["compiler_completed_at"] = _utc_now()
            current.metadata.pop("compiler_handoff", None)

        self.queue.update(job_id, complete)

    def _handoff_metadata(self, handoff: Path) -> dict[str, Any]:
        """Read only a canonical packet created beneath this vault's manual outbox."""
        candidate = Path(handoff)
        if not candidate.is_absolute():
            raise ValueError("compiler handoff must be an absolute vault path")
        manual_root = self.vault.runtime / "manual"
        try:
            self._safe_regular_under(manual_root, candidate)
            relative = candidate.relative_to(self.vault.root).as_posix()
        except (OSError, ValueError) as error:
            raise ValueError("compiler handoff is outside the trusted manual outbox") from error
        if not re.fullmatch(r"\.kb/manual/manual_[0-9a-f]{32}\.json", relative):
            raise ValueError("compiler handoff has an unsafe filename")
        try:
            packet, packet_hash, packet_identity = self._read_pinned_handoff_packet(candidate)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError("compiler handoff packet is unreadable") from error
        if not isinstance(packet, dict) or packet.get("status") != "needs_agent":
            raise ValueError("compiler handoff packet is invalid")
        created_at = packet.get("created_at")
        reason = packet.get("reason", "manual compilation requested")
        self._validate_handoff_record({
            "path": relative,
            "status": "needs_agent",
            "created_at": created_at,
            "reason": reason,
            "sha256": packet_hash,
            "identity": list(packet_identity),
        })
        return {
            "path": relative,
            "status": "needs_agent",
            "created_at": created_at,
            "reason": reason,
            "sha256": packet_hash,
            "identity": list(packet_identity),
        }

    def _read_pinned_handoff_packet(
        self, candidate: Path
    ) -> tuple[object, str, tuple[int, int, int, int]]:
        """Parse the exact regular-file bytes whose digest will bind the handoff."""
        with self._open_pinned_regular(candidate) as (descriptor, identity):
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
                chunks.append(chunk)
            data = b"".join(chunks)
            self._assert_unchanged(candidate, identity)
        return json.loads(data.decode("utf-8")), digest.hexdigest(), identity

    def _mark_compiler_pending(
        self, job_id: str, source: SourceVersion, processed_path: str, handoff: dict[str, Any]
    ) -> None:
        self._validate_handoff_record(handoff)

        def pending(current: Job) -> None:
            history = current.metadata.get("compiler_handoffs", [])
            if not isinstance(history, list):
                raise ValueError("compiler handoff history is invalid")
            for item in history:
                self._validate_handoff_record(item)
            current.state = "pending_attention"
            current.error = "compiler handoff required"
            current.metadata["source"] = asdict(source)
            current.metadata["processed_path"] = processed_path
            current.metadata["compiler_status"] = "needs_agent"
            current.metadata["compiler_handoffs"] = [*history, handoff]
            current.metadata["compiler_handoff"] = handoff["path"]
            current.metadata["compiler_requested_at"] = handoff["created_at"]

        try:
            self.queue.update(job_id, pending)
        except Exception:
            if self._handoff_is_durably_pending(job_id, handoff):
                return
            self._remove_unpersisted_handoff(handoff)
            raise

    def _handoff_is_durably_pending(self, job_id: str, handoff: dict[str, Any]) -> bool:
        try:
            job = self.queue.get(job_id)
            self._validate_pending_compiler_metadata(job)
        except (OSError, ValueError):
            return False
        history = job.metadata["compiler_handoffs"]
        return bool(history) and history[-1] == handoff

    def _remove_unpersisted_handoff(self, handoff: dict[str, Any]) -> None:
        """Remove only the packet this failed atomic marker could not reference."""
        try:
            self._validate_handoff_record(handoff)
            candidate = self.vault.root / Path(*PurePosixPath(handoff["path"]).parts)
            digest, identity = self._hash_pinned_regular(candidate, identity=True)
            if digest != handoff["sha256"] or list(identity) != handoff["identity"]:
                return
            self._remove_if_unchanged(candidate, identity)
        except (OSError, ValueError, json.JSONDecodeError):
            # A missing or modified packet must never trigger a broader cleanup.
            return

    def _validate_pending_compiler_metadata(self, job: Job) -> None:
        if job.state != "pending_attention" or job.metadata.get("compiler_status") != "needs_agent":
            raise ValueError("job is not awaiting a compiler handoff")
        self._validate_handoff_history(job.metadata)
        self._safe_processed_metadata(job.metadata.get("processed_path"))

    def _validate_handoff_history(self, metadata: dict[str, object]) -> None:
        history = metadata.get("compiler_handoffs")
        latest = metadata.get("compiler_handoff")
        if not isinstance(history, list) or not history or not isinstance(latest, str):
            raise ValueError("compiler handoff metadata is invalid")
        for item in history:
            self._validate_handoff_record(item)
        if history[-1]["path"] != latest:
            raise ValueError("compiler handoff metadata has an inconsistent latest path")

    def _validate_handoff_record(self, item: object) -> None:
        if not isinstance(item, dict) or set(item) != {
            "path", "status", "created_at", "reason", "sha256", "identity",
        }:
            raise ValueError("compiler handoff metadata is invalid")
        path = item["path"]
        if not isinstance(path, str) or re.fullmatch(r"\.kb/manual/manual_[0-9a-f]{32}\.json", path) is None:
            raise ValueError("compiler handoff path is invalid")
        candidate = self.vault.root / Path(*PurePosixPath(path).parts)
        try:
            self._safe_regular_under(self.vault.runtime / "manual", candidate)
        except (OSError, ValueError) as error:
            raise ValueError("compiler handoff path is unsafe") from error
        if item["status"] != "needs_agent":
            raise ValueError("compiler handoff status is invalid")
        created_at = item["created_at"]
        try:
            if not isinstance(created_at, str):
                raise ValueError
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("compiler handoff timestamp is invalid") from error
        _safe_compiler_line(item["reason"], "handoff reason", limit=500)
        expected_hash = item["sha256"]
        expected_identity = item["identity"]
        if (not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
                or not isinstance(expected_identity, list) or len(expected_identity) != 4
                or any(isinstance(value, bool) or not isinstance(value, int) for value in expected_identity)):
            raise ValueError("compiler handoff binding is invalid")
        actual_hash, actual_identity = self._hash_pinned_regular(candidate, identity=True)
        if actual_hash != expected_hash or list(actual_identity) != expected_identity:
            raise ValueError("compiler handoff packet no longer matches its binding")

    def _safe_processed_metadata(self, value: object) -> str:
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            raise ValueError("processed path metadata is invalid")
        candidate = self.vault.root / value
        try:
            self._safe_regular_under(self.vault.trash, candidate)
        except (OSError, ValueError) as error:
            raise ValueError("processed path metadata is unsafe") from error
        return candidate.relative_to(self.vault.root).as_posix()

    def _compiler_evidence(self, source: SourceVersion, extraction: dict[str, Any]) -> str:
        if extraction.get("status") != "extracted":
            raise ValueError("only extracted evidence can reach a compiler")
        fragments = extraction.get("fragments")
        if not isinstance(fragments, list):
            raise ValueError("compiler extraction fragments are invalid")
        pieces: list[str] = []
        total = 0
        for fragment in fragments:
            if not isinstance(fragment, dict):
                raise ValueError("compiler extraction fragment is invalid")
            locator = fragment.get("locator")
            text = fragment.get("text")
            if not isinstance(locator, str) or not isinstance(text, str):
                raise ValueError("compiler extraction fragment is invalid")
            piece = f"source_id={source.source_id} locator={locator}\n{text}"
            total += len(piece) + (1 if pieces else 0)
            if total > MAX_EVIDENCE_CHARS:
                raise ValueError("compiler evidence exceeds budget")
            pieces.append(piece)
        return "\n".join(pieces)

    def _validated_compiler_changes(
        self, result: object, source: SourceVersion
    ) -> list[tuple[str, dict[str, Any]]]:
        if not isinstance(result, dict) or set(result) != {"changes"}:
            raise ValueError("compiler result must contain only changes")
        raw_changes = result["changes"]
        if not isinstance(raw_changes, list) or len(raw_changes) > MAX_CHANGES:
            raise ValueError("compiler changes exceed budget")
        validated: list[tuple[str, dict[str, Any]]] = []
        paths: set[str] = set()
        for change in raw_changes:
            if not isinstance(change, dict) or set(change) != _COMPILER_FIELDS:
                raise ValueError("compiler change has missing or unexpected fields")
            relative = _safe_wiki_path(change["path"])
            if relative.casefold() in paths:
                raise ValueError("compiler result has duplicate page paths")
            paths.add(relative.casefold())
            title = _safe_compiler_line(change["title"], "title")
            page_type = _safe_compiler_line(change["type"], "type", limit=40)
            if page_type not in _WIKI_TYPES:
                raise ValueError("compiler type is invalid")
            space = _safe_compiler_line(change["space"], "space", limit=80)
            if space not in _WIKI_SPACES and not (
                space.startswith("project:") and _PROJECT_SLUG.fullmatch(space[8:])
            ):
                raise ValueError("compiler space is invalid")
            confidence = _safe_compiler_line(change["confidence"], "confidence", limit=20)
            if confidence not in _WIKI_CONFIDENCES:
                raise ValueError("compiler confidence is invalid")
            source_ids = change["source_ids"]
            if (not isinstance(source_ids, list) or len(source_ids) != 1
                    or source_ids[0] != source.source_id):
                raise ValueError("compiler change must cite only the current source")
            current_state = _safe_compiler_text(change["current_state"], "current_state")
            conflicts = _safe_compiler_text(change["conflicts"], "conflicts", empty=True, limit=20_000)
            timeline_entry = _safe_compiler_text(change["timeline_entry"], "timeline_entry")
            validated.append((relative, {
                "title": title,
                "type": page_type,
                "space": space,
                "confidence": confidence,
                "source_ids": source_ids,
                "current_state": current_state,
                "conflicts": conflicts,
                "timeline_entry": timeline_entry,
            }))
        return validated

    def _claim(self, job: Job) -> Job:
        claimed_value = job.metadata.get("claimed_path")
        if isinstance(claimed_value, str):
            claimed = Path(claimed_value)
            if claimed.exists():
                claimed_hash = self._hash_pinned_regular(claimed)
                original_value = job.metadata.get("original_source_path")
                if (
                    job.metadata.get("original_preserved") is not True
                    and isinstance(original_value, str)
                    and Path(original_value).exists()
                ):
                    original_hash, identity = self._hash_pinned_regular(
                        Path(original_value), identity=True
                    )
                    if original_hash == claimed_hash:
                        self._remove_if_unchanged(Path(original_value), identity)
                return job
            processed = job.metadata.get("processed_path")
            if isinstance(processed, str) and (self.vault.root / processed).is_file():
                return job
            if isinstance(job.metadata.get("source"), dict):
                source = self._source_for(job)
                assert source is not None
                exact = (
                    self.vault.trash
                    / "processed-inbox"
                    / job.job_id
                    / source.original_name
                )
                try:
                    self._safe_regular_under(self.vault.trash, exact)
                except (OSError, ValueError) as error:
                    raise ValueError("claimed input is missing and processed recovery is unsafe") from error
                if self._hash_pinned_regular(exact) != source.sha256:
                    raise ValueError("processed recovery checksum does not match source")
                relative = str(exact.relative_to(self.vault.root))
                self._mark(
                    job.job_id,
                    "validated",
                    source=job.metadata["source"],
                    processed_path=relative,
                )
                return self.queue.get(job.job_id)
            raise ValueError("claimed input is missing")
        original = Path(os.path.abspath(job.source_path))
        name = original.name
        staging = self._safe_child_directory(self.vault.runtime, "staging")
        job_directory = self._safe_child_directory(staging, job.job_id)
        claimed = job_directory / name
        preserved = False
        if original.exists():
            original_hash, original_identity = self._hash_pinned_regular(
                original, identity=True
            )
        elif os.path.lexists(claimed):
            original_hash, original_identity = self._hash_pinned_regular(
                claimed, identity=True
            )
        else:
            raise FileNotFoundError(original)
        if os.path.lexists(claimed):
            if original.exists() and self._hash_pinned_regular(claimed) != original_hash:
                raise FileExistsError("claimed target contains different file")
        else:
            try:
                self._atomic_claim_no_replace(original, claimed)
            except OSError as error:
                cross_volume = error.errno == getattr(errno, "EXDEV", 18) or getattr(error, "winerror", None) == 17
                if not cross_volume:
                    raise
                self._copy_pinned_no_replace(original, claimed, original_hash)
                preserved = True

        def record(current: Job) -> None:
            current.metadata.update(
                original_source_path=str(original),
                claimed_path=str(claimed),
                original_preserved=preserved,
                original_identity=list(original_identity),
                original_sha256=original_hash,
                phase="claimed",
            )
            current.source_path = str(claimed)
            current.state = "stable"
        return self.queue.update(job.job_id, record)

    def _atomic_claim_no_replace(self, source: Path, target: Path) -> None:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            from .source_store import (
                _windows_close_handle,
                _windows_open_directory,
                _windows_rename_directory_handle,
            )

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ]
            create_file.restype = wintypes.HANDLE
            directory_handles: list[int] = []
            handle = None
            try:
                for path in (
                    self.vault.root,
                    self.vault.runtime,
                    self.vault.staging,
                    target.parent,
                ):
                    directory_handles.append(
                        _windows_open_directory(path, self.vault.root)
                    )
                handle = create_file(
                    str(source),
                    0xC0000000 | 0x00010000,
                    0x00000001,
                    None,
                    3,
                    0x00200000,
                    None,
                )
                if handle == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                info = os.lstat(source)
                if not stat.S_ISREG(info.st_mode) or source.is_symlink():
                    raise ValueError("claim source must be a safe regular file")
                _windows_rename_directory_handle(int(handle), target)
            finally:
                if handle not in (None, ctypes.c_void_p(-1).value):
                    _windows_close_handle(int(handle))
                for directory_handle in reversed(directory_handles):
                    _windows_close_handle(directory_handle)
            return
        from .source_store import _posix_rename_noreplace

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_parent = os.open(source.parent, flags)
        target_parent = os.open(target.parent, flags)
        try:
            _posix_rename_noreplace(source_parent, source.name, target_parent, target.name)
            os.fsync(target_parent)
        finally:
            os.close(target_parent)
            os.close(source_parent)

    def _source_for(self, job: Job) -> SourceVersion | None:
        if "source" not in job.metadata:
            return None
        data = job.metadata["source"]
        if not isinstance(data, dict):
            raise ValueError("resume metadata source is invalid")
        try:
            source = SourceVersion(**data)
        except (TypeError, ValueError) as error:
            raise ValueError("resume metadata source is invalid") from error
        expected = (
            f"10_raw/{source.space}/{source.source_id}/{source.version_id}/"
            f"{source.original_name}"
        )
        if source.relative_path != expected:
            raise ValueError("resume metadata has a non-canonical raw path")
        payload = self.vault.root / source.relative_path
        try:
            self._safe_regular_under(self.vault.raw, payload)
            manifest = payload.parent / "manifest.json"
            self._safe_regular_under(self.vault.raw, manifest)
            stored = self.store._read_manifest(manifest)
        except (OSError, ValueError) as error:
            raise ValueError("resume metadata cannot be verified") from error
        if replace(source, status="archived") != stored:
            raise ValueError("resume metadata disagrees with raw manifest")
        return stored

    def _archive(self, job: Job, space: str) -> SourceVersion:
        incoming = Path(job.source_path)
        latest = self.catalog.latest_source(space, incoming.name)
        source = self.store.archive(incoming, space, source_id=latest.source_id if latest else None, previous_version_id=latest.version_id if latest else None)
        self._mark(job.job_id, "archived", source=asdict(source), space=space)
        return source

    def _extraction_for(self, job: Job, source: SourceVersion) -> dict[str, Any]:
        existing = job.metadata.get("extraction")
        if isinstance(existing, dict) and self._valid_extraction(existing):
            return existing
        result = self.registry.extract(self.vault.root / source.relative_path)
        extraction = {"status": result.status, "warning": result.warning, "fragments": [asdict(fragment) for fragment in result.fragments]}
        if not self._valid_extraction(extraction):
            raise ValueError("extractor returned an invalid extraction")
        self._mark(job.job_id, "extracted", extraction=extraction)
        return extraction

    @staticmethod
    def _valid_extraction(value: dict[str, Any]) -> bool:
        if value.get("status") not in {"extracted", "pending_extractor"}:
            return False
        fragments = value.get("fragments")
        if not isinstance(fragments, list):
            return False
        if value["status"] == "pending_extractor" and fragments:
            raise ValueError("pending_extractor must not contain fragments")
        return all(
            isinstance(item, dict)
            and isinstance(item.get("locator"), str)
            and isinstance(item.get("text"), str)
            for item in fragments
        )

    def _mark(self, job_id: str, state: str, **metadata: object) -> None:
        def apply(job: Job) -> None:
            job.state = state  # type: ignore[assignment]
            job.metadata.update(metadata)
            job.error = None
        self.queue.update(job_id, apply)

    def _cleanup_claim_staging(self, job_id: str) -> None:
        """Remove only this now-empty Task 5 claim directory before a Wiki transaction."""
        job = self.queue.get(job_id)
        claimed_value = job.metadata.get("claimed_path")
        if not isinstance(claimed_value, str):
            return
        claim = Path(claimed_value)
        expected = self.vault.staging / job_id
        if claim.parent != expected:
            raise ValueError("claimed staging path is invalid")
        if not expected.exists():
            return
        self._safe_directory(expected)
        try:
            expected.rmdir()
        except OSError:
            # A non-empty directory can contain only an interrupted claim artifact;
            # leave it for normal recovery rather than deleting unrecognized data.
            return

    def _write_cache(self, source: SourceVersion, extraction: dict[str, Any]) -> None:
        directory = self.vault.index / "cache"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{source.version_id}.json"
        temporary = directory / f".{target.name}.{uuid4().hex}.tmp"
        payload = {"source": asdict(source), "fragments": extraction["fragments"], "warning": extraction.get("warning")}
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            self._sync_directory(directory)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _move_processed(self, job: Job, source: SourceVersion) -> Path:
        self.queue._validate_job_id(job.job_id)
        name = source.original_name
        if not name or name in {".", ".."}:
            raise ValueError("source path must have a safe filename")
        job_directory = self._safe_processed_job_directory(job.job_id)
        target = job_directory / name
        incoming = Path(job.source_path)
        # Keeping these handles open prevents a Windows directory replacement
        # while the path below is resolved; POSIX gets a no-follow directory fd.
        with self._pinned_processed_directory(job_directory):
            if os.path.lexists(target):
                if self._hash_pinned_regular(target) != source.sha256:
                    raise FileExistsError("processed target contains different file")
                if not incoming.exists():
                    return target
                incoming_hash, identity = self._hash_pinned_regular(incoming, identity=True)
                if incoming_hash != source.sha256:
                    raise ValueError("incoming changed after archive; checksum does not match source")
                self._remove_if_unchanged(incoming, identity)
                return target
            if not incoming.exists():
                raise FileNotFoundError(f"incoming file is missing: {incoming}")
            identity = self._copy_pinned_no_replace(incoming, target, source.sha256)
            self._remove_if_unchanged(incoming, identity)
            return target

    @contextmanager
    def _pinned_processed_directory(self, job_directory: Path):
        """Hold the verified processed namespace while publishing its child."""
        if os.name != "nt":
            descriptor = os.open(
                job_directory,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise ValueError("processed path is not a directory")
                yield descriptor
            finally:
                os.close(descriptor)
            return
        # SourceStore already carries the Windows handle implementation used for
        # immutable raw publishing.  Its handles deny DELETE sharing, which pins
        # every directory binding until this transaction finishes.
        from .source_store import _windows_close_handle, _windows_open_directory

        paths = [
            self.vault.root,
            self.vault.trash,
            self.vault.trash / "processed-inbox",
            job_directory,
        ]
        handles: list[int] = []
        try:
            for path in paths:
                handles.append(_windows_open_directory(path, self.vault.root))
            yield None
        finally:
            for handle in reversed(handles):
                _windows_close_handle(handle)

    def _safe_processed_job_directory(self, job_id: str) -> Path:
        """Create only verified non-link children below the vault trash root."""
        self._safe_directory(self.vault.root)
        trash = self._safe_child_directory(self.vault.root, "99_trash")
        processed = self._safe_child_directory(trash, "processed-inbox")
        return self._safe_child_directory(processed, job_id)

    def _safe_child_directory(self, parent: Path, name: str) -> Path:
        self._safe_directory(parent)
        child = parent / name
        if os.path.lexists(child):
            self._safe_directory(child)
            return child
        try:
            os.mkdir(child)
        except FileExistsError:
            pass
        self._safe_directory(child)
        return child

    @staticmethod
    def _safe_directory(path: Path) -> None:
        info = os.lstat(path)
        junction = getattr(path, "is_junction", None)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or (callable(junction) and junction())
        ):
            raise ValueError("processed path contains a link or unsafe directory")

    def _copy_pinned_no_replace(
        self, incoming: Path, target: Path, expected_hash: str
    ) -> tuple[int, int, int, int]:
        """Copy from a no-follow regular descriptor, then link-publish exact target."""
        temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
        identity: tuple[int, int, int, int] | None = None
        try:
            with self._open_pinned_regular(incoming) as (input_fd, identity):
                output_fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                digest = hashlib.sha256()
                try:
                    while chunk := os.read(input_fd, 1024 * 1024):
                        digest.update(chunk)
                        offset = 0
                        while offset < len(chunk):
                            offset += os.write(output_fd, chunk[offset:])
                    os.fsync(output_fd)
                finally:
                    os.close(output_fd)
                if digest.hexdigest() != expected_hash:
                    raise ValueError("incoming changed after archive; checksum does not match source")
                self._assert_unchanged(incoming, identity)
                os.link(temporary, target)
                self._sync_directory(target.parent)
                return identity
        finally:
            # The random temp belongs to this operation; never touch the target.
            temporary.unlink(missing_ok=True)

    def _hash_pinned_regular(
        self, path: Path, *, identity: bool = False
    ) -> str | tuple[str, tuple[int, int, int, int]]:
        with self._open_pinned_regular(path) as (descriptor, token):
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            self._assert_unchanged(path, token)
        result = digest.hexdigest()
        return (result, token) if identity else result

    @contextmanager
    def _open_pinned_regular(self, path: Path):
        before = os.lstat(path)
        junction = getattr(path, "is_junction", None)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or (callable(junction) and junction())
        ):
            raise ValueError("incoming must be a safe regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            token = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != token:
                raise ValueError("incoming changed while opening")
            yield descriptor, token
            after = os.fstat(descriptor)
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != token:
                raise ValueError("incoming changed while reading")
        finally:
            os.close(descriptor)

    @staticmethod
    def _assert_unchanged(path: Path, token: tuple[int, int, int, int]) -> None:
        current = os.lstat(path)
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != token:
            raise ValueError("incoming changed while reading")

    def _remove_if_unchanged(self, path: Path, token: tuple[int, int, int, int]) -> None:
        self._assert_unchanged(path, token)
        path.unlink()
        self._sync_directory(path.parent)

    @staticmethod
    def _safe_regular_under(root: Path, candidate: Path) -> None:
        root = Path(os.path.abspath(os.fspath(root)))
        candidate = Path(os.path.abspath(os.fspath(candidate)))
        try:
            candidate.relative_to(root)
        except ValueError:
            raise ValueError("path escapes trusted root") from None
        current = Path(candidate.anchor)
        for component in candidate.parts[1:]:
            current = current / component
            info = os.lstat(current)
            junction = getattr(current, "is_junction", None)
            if stat.S_ISLNK(info.st_mode) or (callable(junction) and junction()):
                raise ValueError("path contains a link or junction")
        info = os.lstat(candidate)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("path is not a regular file")

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
