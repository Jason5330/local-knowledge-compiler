"""Durable, immutable storage for the raw files behind source versions."""

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import time
from typing import Iterator
from uuid import uuid4

from .models import SourceVersion


_CHUNK_SIZE = 1024 * 1024
_COMPONENT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")
_SOURCE_ID_RE = re.compile(r"src_[a-z0-9][a-z0-9_-]{0,127}\Z")
_VERSION_ID_RE = re.compile(r"ver_[0-9a-f]{64}\Z")
_MANIFEST_NAME = "manifest.json"
_LOCK_NAME = ".archive.lock"
_WINDOWS_FORBIDDEN_FILENAME_CHARACTERS = frozenset('<>:"|?*')
_RESERVED_WINDOWS_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def file_sha256(path: Path) -> str:
    """Return the SHA-256 digest for *path*, without loading it all into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_junction(path: Path) -> bool:
    """Return whether *path* is a Windows junction on supported Python versions."""
    checker = getattr(path, "is_junction", None)
    try:
        return bool(checker()) if callable(checker) else False
    except OSError:
        return True


class SourceStore:
    """Archive source files into a content-addressed, immutable raw-file tree."""

    def __init__(self, raw_root: Path | str) -> None:
        candidate = Path(raw_root)
        if candidate.exists() and (
            candidate.is_symlink() or _is_junction(candidate) or not candidate.is_dir()
        ):
            raise ValueError("raw_root must be a directory and not a symlink")
        candidate.mkdir(parents=True, exist_ok=True)
        self.raw_root = candidate.resolve()

    def archive(
        self,
        incoming: Path | str,
        space: str,
        source_id: str | None = None,
        previous_version_id: str | None = None,
    ) -> SourceVersion:
        """Copy one regular file into the store and return its immutable version."""
        incoming_path = Path(incoming)
        self._validate_input(incoming_path)
        self._validate_component(space, "space")
        if source_id is not None:
            self._validate_source_id(source_id)
        if previous_version_id is not None:
            self._validate_version_id(previous_version_id)
        if previous_version_id is not None and source_id is None:
            raise ValueError("previous_version_id requires source_id")

        original_name = incoming_path.name
        self._validate_filename(original_name)
        source_digest = file_sha256(incoming_path)
        version_id = f"ver_{source_digest}"

        with self._archive_lock():
            duplicate = self._find_by_digest(source_digest)
            if duplicate is not None:
                return duplicate

            actual_source_id = source_id or f"src_{source_digest[:16]}"
            if previous_version_id is not None:
                self._validate_predecessor(space, actual_source_id, previous_version_id)

            source = SourceVersion(
                source_id=actual_source_id,
                version_id=version_id,
                space=space,
                original_name=original_name,
                relative_path=(
                    f"10_raw/{space}/{actual_source_id}/{version_id}/{original_name}"
                ),
                sha256=source_digest,
                media_type=mimetypes.guess_type(original_name)[0]
                or "application/octet-stream",
                status="archived",
                previous_version_id=previous_version_id,
            )
            self._publish(incoming_path, source)
            return source

    def _publish(self, incoming: Path, source: SourceVersion) -> None:
        parent = self._safe_directory(self.raw_root / source.space)
        parent = self._safe_directory(parent / source.source_id)
        target = parent / source.version_id
        self._ensure_contained(target)
        if os.path.lexists(target):
            raise FileExistsError("immutable target already exists")

        stage = parent / f".{source.version_id}.tmp-{uuid4().hex}"
        self._ensure_contained(stage)
        stage.mkdir()
        try:
            copied = stage / source.original_name
            self._copy_with_fsync(incoming, copied)
            if file_sha256(copied) != source.sha256:
                raise ValueError("copied file checksum does not match source")
            self._write_manifest(stage / _MANIFEST_NAME, source)
            self._fsync_directory(stage)
            if os.path.lexists(target):
                raise FileExistsError("immutable target already exists")
            try:
                os.rename(stage, target)
            except FileExistsError as error:
                raise FileExistsError("immutable target already exists") from error
            self._fsync_directory(parent)
        except BaseException:
            if stage.exists():
                shutil.rmtree(stage)
            raise

    @contextmanager
    def _archive_lock(self) -> Iterator[None]:
        """Serialize scanners and publishers without a mutable dedupe index."""
        lock = self.raw_root / _LOCK_NAME
        self._ensure_contained(lock)
        deadline = time.monotonic() + 30
        while True:
            try:
                lock.mkdir()
                break
            except (FileExistsError, PermissionError):
                if not os.path.lexists(lock):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
                    continue
                if lock.is_symlink() or _is_junction(lock):
                    raise ValueError("archive lock is unsafe")
                if not lock.is_dir():
                    if not os.path.lexists(lock):
                        continue
                    raise ValueError("archive lock is unsafe")
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for source archive lock")
                time.sleep(0.01)
        try:
            yield
        finally:
            lock.rmdir()

    def _find_by_digest(self, digest: str) -> SourceVersion | None:
        for space_dir in self.raw_root.iterdir():
            if space_dir.name == _LOCK_NAME:
                continue
            if (
                space_dir.is_symlink()
                or _is_junction(space_dir)
                or not space_dir.is_dir()
            ):
                raise ValueError("raw store contains an unsafe entry")
            self._ensure_contained(space_dir)
            self._validate_component(space_dir.name, "space")
            for source_dir in space_dir.iterdir():
                if (
                    source_dir.is_symlink()
                    or _is_junction(source_dir)
                    or not source_dir.is_dir()
                ):
                    raise ValueError("raw store contains an unsafe source entry")
                self._ensure_contained(source_dir)
                self._validate_source_id(source_dir.name)
                for version_dir in source_dir.iterdir():
                    if version_dir.name.startswith("."):
                        continue
                    if (
                        version_dir.is_symlink()
                        or _is_junction(version_dir)
                        or not version_dir.is_dir()
                    ):
                        raise ValueError("raw store contains an unsafe version entry")
                    self._ensure_contained(version_dir)
                    self._validate_version_id(version_dir.name)
                    manifest = version_dir / _MANIFEST_NAME
                    if not manifest.exists() or manifest.is_symlink():
                        raise ValueError("source manifest is missing")
                    stored = self._read_manifest(manifest)
                    if stored.sha256 == digest:
                        return stored
        return None

    def _validate_predecessor(
        self, space: str, source_id: str, version_id: str
    ) -> None:
        manifest = self.raw_root / space / source_id / version_id / _MANIFEST_NAME
        self._ensure_contained(manifest)
        if not manifest.exists() or manifest.is_symlink():
            raise ValueError("previous_version_id does not exist")
        predecessor = self._read_manifest(manifest)
        if predecessor.space != space or predecessor.source_id != source_id:
            raise ValueError("previous_version_id does not belong to this source")

    def _read_manifest(self, path: Path) -> SourceVersion:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("source manifest is corrupt") from error
        expected_keys = set(SourceVersion.__dataclass_fields__)
        legacy_optional = {"created_sequence"}
        if not isinstance(data, dict) or not set(data) <= expected_keys:
            raise ValueError("source manifest is corrupt")
        missing = expected_keys - set(data)
        if missing - legacy_optional:
            raise ValueError("source manifest is corrupt")
        data.setdefault("created_sequence", None)
        try:
            source = SourceVersion(**data)
            self._validate_manifest(source, path.parent)
        except (TypeError, ValueError) as error:
            raise ValueError("source manifest is corrupt") from error
        return source

    def _validate_manifest(self, source: SourceVersion, version_dir: Path) -> None:
        self._validate_component(source.space, "space")
        self._validate_source_id(source.source_id)
        self._validate_version_id(source.version_id)
        self._validate_filename(source.original_name)
        if source.status != "archived" or not isinstance(source.media_type, str):
            raise ValueError("invalid source manifest fields")
        if not isinstance(source.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source.sha256):
            raise ValueError("invalid source manifest hash")
        if source.version_id != f"ver_{source.sha256}":
            raise ValueError("invalid source manifest version")
        if source.previous_version_id is not None:
            self._validate_version_id(source.previous_version_id)
        if source.created_sequence is not None and (
            isinstance(source.created_sequence, bool)
            or not isinstance(source.created_sequence, int)
            or source.created_sequence <= 0
        ):
            raise ValueError("invalid source manifest sequence")
        expected_relative = (
            f"10_raw/{source.space}/{source.source_id}/{source.version_id}/"
            f"{source.original_name}"
        )
        if source.relative_path != expected_relative:
            raise ValueError("invalid source manifest path")
        if (
            version_dir.name != source.version_id
            or version_dir.parent.name != source.source_id
            or version_dir.parent.parent.name != source.space
        ):
            raise ValueError("source manifest location is invalid")
        content = version_dir / source.original_name
        self._ensure_contained(content)
        if (
            not content.exists()
            or content.is_symlink()
            or _is_junction(content)
            or not content.is_file()
        ):
            raise ValueError("source manifest content is missing")
        if file_sha256(content) != source.sha256:
            raise ValueError("source manifest content checksum does not match")

    @staticmethod
    def _copy_with_fsync(source: Path, destination: Path) -> None:
        with source.open("rb") as input_file, destination.open("xb") as output_file:
            while chunk := input_file.read(_CHUNK_SIZE):
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())

    @staticmethod
    def _write_manifest(path: Path, source: SourceVersion) -> None:
        encoded = json.dumps(asdict(source), sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )
        with path.open("xb") as manifest:
            manifest.write(encoded)
            manifest.write(b"\n")
            manifest.flush()
            os.fsync(manifest.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate_input(path: Path) -> None:
        if path.is_symlink() or not path.exists() or not path.is_file():
            raise ValueError("incoming must be an existing regular file")

    @staticmethod
    def _validate_component(value: str, label: str) -> None:
        if (
            not isinstance(value, str)
            or not _COMPONENT_RE.fullmatch(value)
            or SourceStore._is_windows_reserved(value)
        ):
            raise ValueError(f"invalid {label}")

    @staticmethod
    def _validate_source_id(value: str) -> None:
        if not isinstance(value, str) or not _SOURCE_ID_RE.fullmatch(value):
            raise ValueError("invalid source_id")

    @staticmethod
    def _validate_version_id(value: str) -> None:
        if not isinstance(value, str) or not _VERSION_ID_RE.fullmatch(value):
            raise ValueError("invalid version_id")

    @staticmethod
    def _validate_filename(value: str) -> None:
        if (
            isinstance(value, str)
            and value.rstrip(". ").casefold() == _MANIFEST_NAME
        ):
            raise ValueError("reserved original filename manifest.json")
        if (
            not isinstance(value, str)
            or not value
            or value in {".", ".."}
            or Path(value).name != value
            or "/" in value
            or "\\" in value
            or any(character in _WINDOWS_FORBIDDEN_FILENAME_CHARACTERS for character in value)
            or value.endswith((".", " "))
            or any(ord(character) < 32 for character in value)
            or SourceStore._is_windows_reserved(value)
        ):
            raise ValueError("invalid original filename")

    def _safe_directory(self, path: Path) -> Path:
        self._ensure_contained(path)
        if os.path.lexists(path):
            if path.is_symlink() or _is_junction(path) or not path.is_dir():
                raise ValueError("raw store path is unsafe")
        else:
            path.mkdir()
        self._ensure_contained(path)
        return path

    def _ensure_contained(self, path: Path) -> None:
        if path.is_symlink() or _is_junction(path):
            raise ValueError("raw store path is unsafe")
        try:
            path.resolve(strict=False).relative_to(self.raw_root)
        except (OSError, ValueError) as error:
            raise ValueError("raw store path escapes raw_root") from error

    @staticmethod
    def _is_windows_reserved(value: str) -> bool:
        stem = value.rstrip(". ").split(".", 1)[0].lower()
        return stem in _RESERVED_WINDOWS_NAMES
