"""Safe canonical storage for local correction records and timelines."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from .correction_model import (
    CorrectionRecord,
    record_from_dict,
    record_to_dict,
)
from .paths import VaultPaths
from .queue import shared_writer_lock
from .safety import is_reparse, secure_directory


_CORRECTION_ID = re.compile(r"COR-[0-9]{8}-[0-9a-f]{12}\Z")
_EVENT_TYPES = frozenset(
    {
        "created",
        "occurrence",
        "revalidated",
        "stale",
        "suspended",
        "activated",
        "retired",
        "index_update_failed",
    }
)
_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "correction_id",
        "event_type",
        "actor",
        "reason",
        "created_at",
        "details",
    }
)


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _json_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object contains duplicate keys")
        result[key] = value
    return result


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _safe_line(value: object, label: str, maximum: int = 2_000) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or "\n" in value
        or "\r" in value
        or "\x00" in value
    ):
        raise ValueError(f"{label} must be one bounded safe line")
    return value


@contextmanager
def _pinned_directory(root: Path, directory: Path):
    from .compiler import ManualCompiler

    locker = ManualCompiler(directory, trusted_root=root)
    with locker._pinned_outbox(create=False) as descriptor:
        yield descriptor


@contextmanager
def _open_pinned_regular(
    path: Path,
    *,
    parent_fd: int | None = None,
    name: str | None = None,
):
    before = (
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if parent_fd is not None and name is not None
        else os.lstat(path)
    )
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or is_reparse(path)
        or before.st_nlink != 1
    ):
        raise ValueError("correction file is not a safe regular file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = (
        os.open(name, flags, dir_fd=parent_fd)
        if parent_fd is not None and name is not None
        else os.open(path, flags)
    )
    try:
        opened = os.fstat(descriptor)
        token = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != token:
            raise ValueError("correction file changed while opening")
        yield descriptor, token
    finally:
        os.close(descriptor)


class CorrectionStore:
    MAX_RECORD_BYTES = 64_000
    MAX_TIMELINE_BYTES = 2_000_000
    MAX_EVENT_BYTES = 16_000

    def __init__(self, vault: VaultPaths | Path | str) -> None:
        if isinstance(vault, VaultPaths):
            paths = vault
        else:
            candidate = Path(os.path.abspath(os.fspath(vault)))
            paths = VaultPaths(candidate)
        root = Path(os.path.abspath(os.fspath(paths.root)))
        if not root.is_dir() or root.is_symlink() or is_reparse(root):
            raise ValueError("correction vault root is unsafe")
        self.paths = VaultPaths(root)
        secure_directory(self.paths.correction_records)
        secure_directory(self.paths.correction_timeline)

    @staticmethod
    def _validate_id(correction_id: str) -> str:
        if (
            not isinstance(correction_id, str)
            or _CORRECTION_ID.fullmatch(correction_id) is None
        ):
            raise ValueError("invalid correction_id")
        return correction_id

    def _record_path(self, correction_id: str) -> Path:
        return self.paths.correction_records / (
            f"{self._validate_id(correction_id)}.json"
        )

    def _timeline_path(self, correction_id: str) -> Path:
        return self.paths.correction_timeline / (
            f"{self._validate_id(correction_id)}.jsonl"
        )

    def _read_bytes(self, path: Path, maximum: int) -> bytes:
        try:
            with _pinned_directory(
                self.paths.root,
                path.parent,
            ) as parent_fd:
                with _open_pinned_regular(
                    path,
                    parent_fd=parent_fd,
                    name=path.name,
                ) as (descriptor, token):
                    if token[2] > maximum:
                        raise ValueError("correction file exceeds size limit")
                    chunks = bytearray()
                    while chunk := os.read(
                        descriptor,
                        min(65_536, maximum + 1 - len(chunks)),
                    ):
                        chunks.extend(chunk)
                        if len(chunks) > maximum:
                            raise ValueError(
                                "correction file exceeds size limit"
                            )
                    return bytes(chunks)
        except FileNotFoundError:
            raise
        except ValueError:
            raise
        except OSError as error:
            raise ValueError("correction file is unsafe") from error

    def _decode_record(self, payload: bytes) -> CorrectionRecord:
        try:
            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_json_no_duplicates,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("correction record contains invalid JSON") from error
        return record_from_dict(value)

    def get(self, correction_id: str) -> CorrectionRecord:
        path = self._record_path(correction_id)
        record = self._decode_record(
            self._read_bytes(path, self.MAX_RECORD_BYTES)
        )
        if record.correction_id != correction_id:
            raise ValueError("correction record filename does not match")
        return record

    def _write_temporary(self, directory: Path, payload: bytes) -> Path:
        name = f".correction-{uuid4().hex}.tmp"
        path = directory / name
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags, 0o600)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short correction write")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return path

    def create(self, record: CorrectionRecord) -> CorrectionRecord:
        payload = _canonical(record_to_dict(record))
        if len(payload) > self.MAX_RECORD_BYTES:
            raise ValueError("correction record exceeds size limit")
        destination = self._record_path(record.correction_id)
        temporary = self._write_temporary(destination.parent, payload)
        try:
            try:
                if os.name == "nt":
                    os.rename(temporary, destination)
                else:
                    os.link(temporary, destination)
            except FileExistsError:
                raise
            except OSError as error:
                if destination.exists():
                    raise FileExistsError(destination) from error
                raise
        finally:
            temporary.unlink(missing_ok=True)
        return self.get(record.correction_id)

    def replace(
        self,
        record: CorrectionRecord,
        *,
        expected_hash: str,
    ) -> CorrectionRecord:
        current = self.get(record.correction_id)
        if current.content_sha256 != expected_hash:
            raise ValueError("correction changed before replacement")
        payload = _canonical(record_to_dict(record))
        if len(payload) > self.MAX_RECORD_BYTES:
            raise ValueError("correction record exceeds size limit")
        destination = self._record_path(record.correction_id)
        temporary = self._write_temporary(destination.parent, payload)
        try:
            if self.get(record.correction_id).content_sha256 != expected_hash:
                raise ValueError("correction changed before replacement")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return self.get(record.correction_id)

    def iter_records(
        self,
        *,
        max_records: int = 10_000,
        max_bytes: int = 64_000_000,
    ) -> tuple[list[CorrectionRecord], bool]:
        if (
            isinstance(max_records, bool)
            or not isinstance(max_records, int)
            or max_records < 1
            or isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1
        ):
            raise ValueError("correction scan budgets are invalid")
        records = []
        consumed = 0
        truncated = False
        with os.scandir(self.paths.correction_records) as entries:
            safe_entries = []
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue
                if len(safe_entries) >= max_records + 1:
                    truncated = True
                    break
                info = os.lstat(entry.path)
                if (
                    entry.is_symlink()
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or is_reparse(Path(entry.path))
                ):
                    raise ValueError("correction record entry is unsafe")
                safe_entries.append((entry.name, info.st_size))
        safe_entries.sort(key=lambda item: item[0])
        for name, size in safe_entries:
            if len(records) >= max_records or consumed + size > max_bytes:
                truncated = True
                break
            consumed += size
            records.append(self.get(name[:-5]))
        return records, truncated

    def _event(
        self,
        correction_id: str,
        *,
        event_type: str,
        actor: str,
        reason: str,
        details: dict[str, object],
    ) -> dict[str, object]:
        self._validate_id(correction_id)
        if event_type not in _EVENT_TYPES:
            raise ValueError("invalid correction event type")
        if not isinstance(details, dict):
            raise ValueError("event details must be an object")
        return {
            "schema_version": 1,
            "event_id": uuid4().hex,
            "correction_id": correction_id,
            "event_type": event_type,
            "actor": _safe_line(actor, "event actor", 100),
            "reason": _safe_line(reason, "event reason"),
            "created_at": _now(),
            "details": details,
        }

    def append_event(
        self,
        correction_id: str,
        *,
        event_type: str,
        actor: str,
        reason: str,
        details: dict[str, object],
    ) -> dict[str, object]:
        with shared_writer_lock(
            self.paths.runtime / "write.lock",
            timeout=0,
        ):
            return self._append_event_unlocked(
                correction_id,
                event_type=event_type,
                actor=actor,
                reason=reason,
                details=details,
            )

    def _append_event_unlocked(
        self,
        correction_id: str,
        *,
        event_type: str,
        actor: str,
        reason: str,
        details: dict[str, object],
    ) -> dict[str, object]:
        """Append while the caller already owns the Vault writer lock."""
        self.get(correction_id)
        event = self._event(
            correction_id,
            event_type=event_type,
            actor=actor,
            reason=reason,
            details=details,
        )
        payload = _canonical(event)
        if len(payload) > self.MAX_EVENT_BYTES:
            raise ValueError("correction event exceeds size limit")
        path = self._timeline_path(correction_id)
        existing_size = path.stat().st_size if path.exists() else 0
        if existing_size + len(payload) > self.MAX_TIMELINE_BYTES:
            raise ValueError("correction timeline exceeds size limit")
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("correction timeline is unsafe")
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short timeline write")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return event

    def _validate_event(
        self,
        value: object,
        correction_id: str,
    ) -> dict[str, object]:
        if not isinstance(value, dict) or set(value) != _EVENT_FIELDS:
            raise ValueError("timeline event has invalid fields")
        if (
            value["schema_version"] != 1
            or value["correction_id"] != correction_id
            or value["event_type"] not in _EVENT_TYPES
            or not isinstance(value["details"], dict)
        ):
            raise ValueError("timeline event is invalid")
        _safe_line(value["event_id"], "event_id", 64)
        _safe_line(value["actor"], "event actor", 100)
        _safe_line(value["reason"], "event reason")
        _safe_line(value["created_at"], "event timestamp", 40)
        return value

    def events(
        self,
        correction_id: str,
        *,
        max_events: int = 10_000,
    ) -> list[dict[str, object]]:
        if (
            isinstance(max_events, bool)
            or not isinstance(max_events, int)
            or max_events < 1
        ):
            raise ValueError("max_events is invalid")
        path = self._timeline_path(correction_id)
        try:
            payload = self._read_bytes(path, self.MAX_TIMELINE_BYTES)
        except FileNotFoundError:
            return []
        lines = payload.splitlines()
        if len(lines) > max_events:
            raise ValueError("correction timeline exceeds event limit")
        result = []
        for line in lines:
            try:
                value = json.loads(
                    line.decode("utf-8"),
                    object_pairs_hook=_json_no_duplicates,
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError(
                    "correction timeline contains invalid JSON"
                ) from error
            result.append(self._validate_event(value, correction_id))
        return result
