"""Turn one durable inbox job into raw evidence, catalog rows and cache data."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any
from uuid import uuid4

from .catalog import Catalog
from .extractors import registry as default_registry
from .models import Job, SourceVersion
from .paths import VaultPaths
from .queue import DiskQueue
from .source_store import SourceStore


class IngestService:
    def __init__(self, vault: VaultPaths | Path | str, queue: DiskQueue, catalog: Catalog, *, registry: Any = None) -> None:
        self.vault = vault if isinstance(vault, VaultPaths) else VaultPaths(Path(vault).resolve())
        self.queue = queue
        self.catalog = catalog
        self.registry = registry or default_registry
        self.store = SourceStore(self.vault.raw)

    def process(self, job_id: str, *, space: str = "unclassified") -> SourceVersion:
        """Process one job; persist each recoverable boundary before advancing."""
        job = self.queue.get(job_id)
        try:
            self.catalog.initialize()
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
            self._mark(job_id, "published", source=asdict(final), processed_path=str(processed.relative_to(self.vault.root)))
            return final
        except BaseException as error:
            self.queue.fail(job_id, error)
            raise

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
