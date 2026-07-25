"""Validate grounded answers, save them, and queue derived knowledge updates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
from uuid import uuid4

from .models import Job
from .paths import VaultPaths
from .queue import DiskQueue
from .query import _is_reparse, _open_pinned_regular, _pinned_directory


MAX_INPUT_BYTES = 256_000
MAX_QUESTION_CHARS = 16_000
MAX_ANSWER_CHARS = 64_000
MAX_CITATIONS = 128
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
_CONFIDENCE = frozenset({"high", "medium", "low"})


@dataclass(frozen=True)
class FinalizeResult:
    path: Path
    job_id: str
    raw_source_ids: tuple[str, ...]


@dataclass(frozen=True)
class _ValidatedAnswer:
    question: str
    conclusion: str
    confidence: str
    conflicts: str
    citations: tuple[dict[str, object], ...]
    raw_source_ids: tuple[str, ...]


def read_json_document(path: Path | str) -> dict[str, object]:
    """Read one bounded regular JSON object without following a file link."""
    candidate = Path(path).absolute()
    try:
        with _pinned_directory(candidate.parent, candidate.parent) as parent_fd:
            with _open_pinned_regular(
                candidate, parent_fd=parent_fd, name=candidate.name
            ) as (descriptor, token):
                if token[2] > MAX_INPUT_BYTES:
                    raise ValueError("JSON input exceeds size limit")
                chunks = bytearray()
                while chunk := os.read(
                    descriptor, min(65_536, MAX_INPUT_BYTES + 1 - len(chunks))
                ):
                    chunks.extend(chunk)
                    if len(chunks) > MAX_INPUT_BYTES:
                        raise ValueError("JSON input exceeds size limit")
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as error:
        raise ValueError("JSON input must be a safe regular file") from error
    try:
        value = json.loads(bytes(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("JSON input must contain one UTF-8 object") from error
    if not isinstance(value, dict):
        raise ValueError("JSON input must contain one object")
    return value


def finalize_answer(vault: VaultPaths | Path | str, packet: dict, answer: dict) -> Path:
    """Save a validated derived answer without adding it to the raw catalog."""
    paths = _vault_paths(vault)
    validated = _validate(packet, answer)
    path, _ = _write_answer(paths, _render(validated))
    return path


def finalize_and_enqueue(
    vault: VaultPaths | Path | str,
    queue: DiskQueue,
    packet: dict,
    answer: dict,
) -> FinalizeResult:
    """Save an answer and atomically create its fully-typed derived queue job."""
    paths = _vault_paths(vault)
    validated = _validate(packet, answer)
    path, identity = _write_answer(paths, _render(validated))
    relative = path.relative_to(paths.root).as_posix()
    try:
        job = _enqueue_derived_update(queue, relative, validated.raw_source_ids)
    except BaseException:
        _remove_exact_answer(paths, path, identity)
        raise
    return FinalizeResult(path=path, job_id=job.job_id, raw_source_ids=validated.raw_source_ids)


def _vault_paths(vault: VaultPaths | Path | str) -> VaultPaths:
    paths = vault if isinstance(vault, VaultPaths) else VaultPaths(Path(vault).absolute())
    root = paths.root.absolute()
    if not root.is_dir() or root.is_symlink() or _is_reparse(root):
        raise ValueError("vault must be a safe existing directory")
    return VaultPaths(root)


def _validate(packet: object, answer: object) -> _ValidatedAnswer:
    if not isinstance(packet, dict) or not isinstance(answer, dict):
        raise TypeError("packet and answer must be objects")
    question = _safe_text(packet.get("question"), "question", MAX_QUESTION_CHARS, allow_empty=False)
    evidence = packet.get("evidence")
    if not isinstance(evidence, list):
        raise TypeError("packet evidence must be a list")
    if len(evidence) > 256:
        raise ValueError("packet contains too much evidence")

    allowed: dict[tuple[object, ...], tuple[dict[str, object], tuple[str, ...]]] = {}
    legacy: dict[str, list[tuple[dict[str, object], tuple[str, ...]]]] = {}
    for item in evidence:
        identity, public, raw_ids = _evidence_identity(item)
        if identity in allowed:
            raise ValueError("packet contains duplicate evidence")
        allowed[identity] = (public, raw_ids)
        if identity[0] == "legacy":
            legacy.setdefault(str(identity[1]), []).append((public, raw_ids))

    citations = answer.get("citations", [])
    if not isinstance(citations, list):
        raise TypeError("citations must be a list")
    if len(citations) > MAX_CITATIONS:
        raise ValueError("too many citations")
    selected: list[dict[str, object]] = []
    raw_source_ids: list[str] = []
    seen: set[tuple[object, ...]] = set()
    for citation in citations:
        if isinstance(citation, str):
            source_id = _safe_id(citation, "citation source_id")
            choices = legacy.get(source_id, [])
            if len(choices) != 1:
                raise ValueError(f"unknown citation: {source_id}")
            identity = ("legacy", source_id)
            public, raw_ids = choices[0]
        elif isinstance(citation, dict):
            identity, public = _citation_identity(citation)
            match = allowed.get(identity)
            if match is None:
                raise ValueError(f"unknown citation: {public}")
            _, raw_ids = match
        else:
            raise TypeError("citation must be a string or object")
        if identity in seen:
            raise ValueError("duplicate citation")
        seen.add(identity)
        selected.append(public)
        raw_source_ids.extend(raw_ids)

    conclusion = _safe_text(answer.get("conclusion", "目前無法確定"), "conclusion", MAX_ANSWER_CHARS)
    conflicts = _safe_text(answer.get("conflicts", "無"), "conflicts", MAX_ANSWER_CHARS)
    confidence = answer.get("confidence", "low")
    if not isinstance(confidence, str) or confidence not in _CONFIDENCE:
        raise ValueError("confidence must be high, medium, or low")
    return _ValidatedAnswer(
        question=question,
        conclusion=conclusion,
        confidence=confidence,
        conflicts=conflicts,
        citations=tuple(selected),
        raw_source_ids=tuple(dict.fromkeys(raw_source_ids)),
    )


def _evidence_identity(item: object) -> tuple[tuple[object, ...], dict[str, object], tuple[str, ...]]:
    if not isinstance(item, dict):
        raise TypeError("packet evidence item must be an object")
    kind = item.get("kind")
    if kind in (None, "raw_fragment") and "source_id" in item:
        source_id = _safe_id(item.get("source_id"), "evidence source_id")
        if "version_id" not in item and "locator" not in item and kind is None:
            public = {"source_id": source_id}
            return ("legacy", source_id), public, (source_id,)
        version_id = _safe_id(item.get("version_id"), "evidence version_id")
        locator = _safe_locator(item.get("locator"), "evidence locator")
        public = {"source_id": source_id, "version_id": version_id, "locator": locator}
        return ("raw", source_id, version_id, locator), public, (source_id,)
    if kind == "derived_wiki":
        path = _safe_relative_path(item.get("path"), "evidence path", prefix="20_wiki/")
        locator = _safe_locator(item.get("locator"), "evidence locator")
        source_ids = _source_ids(item.get("source_ids"), "evidence source_ids")
        public = {"path": path, "locator": locator, "source_ids": list(source_ids)}
        return ("wiki", path, locator, source_ids), public, source_ids
    raise ValueError("packet evidence has an unsupported provenance shape")


def _citation_identity(citation: dict) -> tuple[tuple[object, ...], dict[str, object]]:
    keys = set(citation)
    if keys == {"source_id", "version_id", "locator"}:
        source_id = _safe_id(citation["source_id"], "citation source_id")
        version_id = _safe_id(citation["version_id"], "citation version_id")
        locator = _safe_locator(citation["locator"], "citation locator")
        public = {"source_id": source_id, "version_id": version_id, "locator": locator}
        return ("raw", source_id, version_id, locator), public
    if keys == {"path", "locator", "source_ids"}:
        path = _safe_relative_path(citation["path"], "citation path", prefix="20_wiki/")
        locator = _safe_locator(citation["locator"], "citation locator")
        source_ids = _source_ids(citation["source_ids"], "citation source_ids")
        public = {"path": path, "locator": locator, "source_ids": list(source_ids)}
        return ("wiki", path, locator, source_ids), public
    raise ValueError("citation must contain an exact provenance identity")


def _source_ids(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 32:
        raise ValueError(f"{label} must be a non-empty bounded list")
    result = tuple(_safe_id(item, label) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} contains duplicates")
    return result


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _safe_text(value: object, label: str, limit: int, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    if len(value) > limit or "\x00" in value or any(ord(char) == 127 for char in value):
        raise ValueError(f"{label} is unsafe or too long")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not allow_empty and not normalized.strip():
        raise ValueError(f"{label} must not be empty")
    return normalized


def _safe_locator(value: object, label: str) -> str:
    locator = _safe_text(value, label, 2_000, allow_empty=False)
    if "\n" in locator or "\t" in locator or any(ord(char) < 32 for char in locator):
        raise ValueError(f"{label} must be one safe line")
    return locator


def _safe_relative_path(value: object, label: str, *, prefix: str) -> str:
    text = _safe_text(value, label, 2_000, allow_empty=False)
    if "\\" in text or not text.startswith(prefix):
        raise ValueError(f"{label} is invalid")
    path = Path(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} is invalid")
    return text


def _markdown(value: str) -> str:
    lines = []
    for line in value.split("\n"):
        escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if escaped.strip() == "---":
            escaped = escaped.replace("---", r"\---", 1)
        lines.append(escaped)
    return "\n".join(lines)


def _render(answer: _ValidatedAnswer) -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    question_yaml = json.dumps(answer.question, ensure_ascii=False)
    citations = (
        "\n".join(
            f"- `{json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}`"
            for item in answer.citations
        )
        or "- 無可用來源"
    )
    return (
        "---\n"
        "type: derived-answer\n"
        "label: 衍生知識\n"
        f"confidence: {answer.confidence}\n"
        f"created_at: {now}\n"
        f"question: {question_yaml}\n"
        "---\n\n"
        "# 問題\n\n"
        f"{_markdown(answer.question)}\n\n"
        "## 回答結論\n\n"
        f"{_markdown(answer.conclusion)}\n\n"
        "## 證據引用\n\n"
        f"{citations}\n\n"
        "## 衝突與限制\n\n"
        f"{_markdown(answer.conflicts)}\n"
    )


def _answers_year(paths: VaultPaths) -> Path:
    year = datetime.now(timezone.utc).strftime("%Y")
    return paths.answers / year


def _write_answer(paths: VaultPaths, content: str) -> tuple[Path, tuple[int, int, int, int]]:
    from .compiler import ManualCompiler

    year = _answers_year(paths)
    locker = ManualCompiler(year, trusted_root=paths.root)
    encoded = content.encode("utf-8")
    with locker._pinned_outbox() as directory_fd:
        for _ in range(16):
            name = f"{datetime.now(timezone.utc):%Y-%m-%d}-{uuid4().hex[:8]}.md"
            temporary_name = f".{name}.{uuid4().hex}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = None
            try:
                descriptor = (
                    os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
                    if directory_fd is not None
                    else os.open(year / temporary_name, flags, 0o600)
                )
                offset = 0
                while offset < len(encoded):
                    offset += os.write(descriptor, encoded[offset:])
                os.fsync(descriptor)
                info = os.fstat(descriptor)
                identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                os.close(descriptor)
                descriptor = None
                if directory_fd is not None:
                    os.link(
                        temporary_name,
                        name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                else:
                    os.link(year / temporary_name, year / name)
                if directory_fd is not None:
                    os.fsync(directory_fd)
                return year / name, identity
            except FileExistsError:
                continue
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    if directory_fd is not None:
                        os.unlink(temporary_name, dir_fd=directory_fd)
                    else:
                        (year / temporary_name).unlink(missing_ok=True)
                except FileNotFoundError:
                    pass
    raise RuntimeError("unable to allocate a unique answer path")


def _enqueue_derived_update(
    queue: DiskQueue, answer_path: str, source_ids: tuple[str, ...]
) -> Job:
    method = getattr(queue, "enqueue_derived_update", None)
    if callable(method):
        return method(answer_path=answer_path, raw_source_ids=list(source_ids))
    if not isinstance(queue, DiskQueue):
        raise TypeError("queue must support derived updates")
    identifier = uuid4().hex
    job = Job(
        job_id=identifier,
        source_path="",
        metadata={
            "job_type": "derived_update",
            "answer_path": answer_path,
            "raw_source_ids": list(source_ids),
        },
    )
    with queue._locked():
        path = queue._job_path(identifier)
        if path.exists():
            raise FileExistsError(f"job already exists: {identifier}")
        queue._write(path, job)
    return queue._copy(job)


def _remove_exact_answer(
    paths: VaultPaths, path: Path, identity: tuple[int, int, int, int]
) -> None:
    from .compiler import ManualCompiler

    try:
        locker = ManualCompiler(path.parent, trusted_root=paths.root)
        with locker._pinned_outbox(create=False) as directory_fd:
            info = (
                os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
                if directory_fd is not None
                else os.lstat(path)
            )
            current = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if current != identity or not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise RuntimeError("queue failed and the saved answer changed before rollback")
            if directory_fd is not None:
                os.unlink(path.name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            else:
                path.unlink()
    except FileNotFoundError:
        return
