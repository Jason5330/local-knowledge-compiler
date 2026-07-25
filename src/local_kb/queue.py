"""Small durable on-disk queue for inbox ingestion jobs."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable, Iterator, get_args
from uuid import uuid4

from .models import Job, JobState
from .source_store import (
    _is_junction,
    _windows_close_handle,
    _windows_open_directory,
)


_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_JOB_STATES = frozenset(get_args(JobState))
MAX_JOB_BYTES = 4 * 1024 * 1024
MAX_QUEUE_BATCH_BYTES = 64 * 1024 * 1024
DEFAULT_QUEUE_BATCH_BYTES = 16 * 1024 * 1024
_WRITER_LOCK_FORMAT = "local-kb-writer-lock-v1"
_MAX_WRITER_LOCK_BYTES = 4096


class _BatchBudgetExceeded(Exception):
    pass


class WriterLock:
    """Kernel-backed single-writer lock with a persistent diagnostic record.

    The pathname is never deleted on release.  This avoids stale-lock recovery
    races entirely: the kernel lock is authoritative, while the JSON record is
    only an owner diagnostic that is refreshed after each successful acquire.
    """

    def __init__(self, path: Path | str, timeout: float = 30) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout < 0
            or timeout > 3600
        ):
            raise ValueError("timeout must be between 0 and 3600 seconds")
        self.path = Path(os.path.abspath(os.fspath(path)))
        if self.path.name in {"", ".", ".."}:
            raise ValueError("writer lock path is unsafe")
        self.timeout = float(timeout)
        self.handle: int | None = None
        self._root: DiskQueue | None = None
        self._token: str | None = None

    def __enter__(self) -> "WriterLock":
        deadline = time.monotonic() + self.timeout
        root = DiskQueue(self.path.parent)
        self._root = root
        created = False
        try:
            while True:
                flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
                try:
                    descriptor = root._open_entry(
                        self.path.name,
                        flags | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                    created = True
                except FileExistsError:
                    descriptor = root._open_entry(
                        self.path.name,
                        flags | getattr(os, "O_NOFOLLOW", 0),
                    )
                    created = False
                if self._try_lock(descriptor):
                    self.handle = descriptor
                    break
                os.close(descriptor)
                if time.monotonic() >= deadline:
                    raise TimeoutError("writer lock unavailable")
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

            if not created:
                existing = self._read_record(self.handle)
                if existing.get("format") != _WRITER_LOCK_FORMAT:
                    raise ValueError("writer lock is not a local knowledge lock")
            self._token = uuid4().hex
            record = {
                "format": _WRITER_LOCK_FORMAT,
                "pid": os.getpid(),
                "token": self._token,
            }
            encoded = _bounded_lock_json(record)
            os.lseek(self.handle, 0, os.SEEK_SET)
            os.ftruncate(self.handle, 0)
            offset = 0
            while offset < len(encoded):
                offset += os.write(self.handle, encoded[offset:])
            os.fsync(self.handle)
            root._sync_directory()
            return self
        except BaseException:
            self._release()
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        self._release()

    @staticmethod
    def _try_lock(descriptor: int) -> bool:
        if os.name == "nt":
            import msvcrt

            # Lock a byte beyond the bounded JSON record so diagnostics remain
            # readable while another process owns the writer lock.
            os.lseek(descriptor, _MAX_WRITER_LOCK_BYTES, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    @staticmethod
    def _read_record(descriptor: int) -> dict[str, object]:
        size = os.fstat(descriptor).st_size
        if size < 1 or size > _MAX_WRITER_LOCK_BYTES:
            raise ValueError("writer lock is not a local knowledge lock")
        os.lseek(descriptor, 0, os.SEEK_SET)
        encoded = DiskQueue._read_descriptor(descriptor, _MAX_WRITER_LOCK_BYTES + 1)
        try:
            value = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("writer lock is not a local knowledge lock") from error
        if (
            not isinstance(value, dict)
            or set(value) != {"format", "pid", "token"}
            or value.get("format") != _WRITER_LOCK_FORMAT
            or isinstance(value.get("pid"), bool)
            or not isinstance(value.get("pid"), int)
            or not isinstance(value.get("token"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", value["token"])
        ):
            raise ValueError("writer lock is not a local knowledge lock")
        return value

    def _release(self) -> None:
        descriptor, self.handle = self.handle, None
        if descriptor is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, _MAX_WRITER_LOCK_BYTES, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        root, self._root = self._root, None
        if root is not None:
            root.close()


def _bounded_lock_json(value: object) -> bytes:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(encoded) > _MAX_WRITER_LOCK_BYTES:
        raise ValueError("writer lock record exceeds size limit")
    return encoded


def _bounded_json(value: object) -> str:
    chunks=[]; size=0
    encoder=json.JSONEncoder(ensure_ascii=False,sort_keys=True,separators=(",", ":"),allow_nan=False)
    for chunk in encoder.iterencode(value):
        size += len(chunk.encode("utf-8"))
        if size > MAX_JOB_BYTES:
            raise ValueError("job JSON exceeds size limit")
        chunks.append(chunk)
    return "".join(chunks)


class DiskQueue:
    """Persist jobs as atomically-replaced JSON files.

    The queue lock covers read-modify-write operations, so two worker processes
    cannot lose a retry increment even though each job is a separate file.
    """

    def __init__(self, root: Path | str, max_retries: int = 3) -> None:
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 1:
            raise ValueError("max_retries must be a positive integer")
        self.root = Path(os.path.abspath(os.fspath(root)))
        self._root_descriptor = self._open_queue_root(self.root)
        self.max_retries = max_retries
        self._lock_path = self.root / ".queue.lock"

    def close(self) -> None:
        descriptor = getattr(self, "_root_descriptor", None)
        if descriptor is None:
            return
        self._root_descriptor = None
        if os.name == "nt":
            _windows_close_handle(descriptor)
        else:
            os.close(descriptor)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass

    def enqueue(self, source_path: Path | str, *, job_id: str | None = None) -> Job:
        identifier = job_id or uuid4().hex
        self._validate_job_id(identifier)
        job = Job(job_id=identifier, source_path=os.fspath(source_path))
        try:
            candidate = Path(source_path)
            info = os.stat(candidate, follow_symlinks=False)
            if candidate.is_file() and not candidate.is_symlink():
                job.metadata["enqueued_identity"] = [
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                ]
                job.metadata["enqueued_sha256"] = hashlib.sha256(
                    candidate.read_bytes()
                ).hexdigest()
        except OSError:
            pass
        with self._locked():
            path = self._job_path(identifier)
            if self._entry_exists(path.name):
                raise FileExistsError(f"job already exists: {identifier}")
            self._write(path, job)
        return self._copy(job)

    def get(self, job_id: str) -> Job:
        with self._locked():
            return self._read(self._job_path(job_id))

    def iter_jobs(self) -> list[Job]:
        with self._locked():
            scan_target = (
                self.root
                if os.name == "nt"
                else self._require_root_descriptor()
            )
            with os.scandir(scan_target) as entries:
                names = sorted(
                    entry.name
                    for entry in entries
                    if entry.name.endswith(".json")
                )
            return [self._read(self.root / name) for name in names]

    def iter_jobs_bounded(self, max_jobs: int, max_bytes: int = DEFAULT_QUEUE_BATCH_BYTES) -> tuple[list[Job], bool]:
        if isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or max_jobs < 1:
            raise ValueError("max_jobs must be a positive integer")
        if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
                or not 1 <= max_bytes <= MAX_QUEUE_BATCH_BYTES):
            raise ValueError("max_bytes must be a positive bounded integer")
        with self._locked():
            paths: list[Path] = []
            scanned=0; scan_truncated=False
            scan_target = (
                self.root
                if os.name == "nt"
                else self._require_root_descriptor()
            )
            with os.scandir(scan_target) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > max_jobs + 1:
                        scan_truncated=True; break
                    if not entry.name.endswith(".json"):
                        continue
                    if len(paths) >= max_jobs:
                        scan_truncated=True; break
                    paths.append(Path(entry.path))
            jobs: list[Job]=[]; used=0
            for path in sorted(paths):
                try:
                    job, actual_size=self._read_pinned(path,max_bytes-used)
                except _BatchBudgetExceeded:
                    return jobs, True
                jobs.append(job); used += actual_size
            return jobs, scan_truncated

    def active_for_source(self, source_path: Path | str) -> Job | None:
        wanted = os.path.normcase(os.path.abspath(os.fspath(source_path)))
        for job in self.iter_jobs():
            original = job.metadata.get("original_source_path", job.source_path)
            if os.path.normcase(os.path.abspath(str(original))) != wanted:
                continue
            if job.state not in {"pending_attention", "published"}:
                return job
            if job.state == "pending_attention":
                expected = job.metadata.get("enqueued_identity")
                if not isinstance(expected, list):
                    return job
                try:
                    info = os.stat(wanted, follow_symlinks=False)
                    current = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
                    if current == expected:
                        digest = hashlib.sha256(Path(wanted).read_bytes()).hexdigest()
                        if digest == job.metadata.get("enqueued_sha256"):
                            return job
                except OSError:
                    pass
                continue
            if job.state == "published" and job.metadata.get("original_preserved") is True:
                try:
                    info = os.stat(wanted, follow_symlinks=False)
                    identity = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
                    if identity != job.metadata.get("original_identity"):
                        continue
                    digest = hashlib.sha256(Path(wanted).read_bytes()).hexdigest()
                    if digest == job.metadata.get("original_sha256"):
                        return job
                except OSError:
                    continue
        return None

    def update(self, job_id: str, change: Callable[[Job], Job | None]) -> Job:
        """Atomically apply *change* to a fresh job copy and persist it."""
        with self._locked():
            path = self._job_path(job_id)
            job = self._read(path)
            result = change(job)
            if result is not None:
                job = result
            if job.job_id != job_id:
                raise ValueError("job update cannot change job_id")
            self._write(path, job)
            return self._copy(job)

    def fail(self, job_id: str, error: BaseException) -> Job:
        message = str(error) or error.__class__.__name__

        def record(job: Job) -> None:
            job.attempts += 1
            job.error = message
            job.state = (
                "pending_attention" if job.attempts >= self.max_retries else "retrying"
            )

        return self.update(job_id, record)

    def _job_path(self, job_id: str) -> Path:
        self._validate_job_id(job_id)
        return self.root / f"{job_id}.json"

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise ValueError("job_id must be a canonical safe identifier")

    def _read(self, path: Path) -> Job:
        return self._read_pinned(path, MAX_JOB_BYTES)[0]

    def _read_pinned(self, path: Path, budget: int) -> tuple[Job, int]:
        if budget < 1:
            raise _BatchBudgetExceeded
        try:
            before = self._entry_stat(path.name)
            if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_size > MAX_JOB_BYTES:
                raise ValueError(f"corrupt job JSON: {path.name}")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = self._open_entry(path.name, flags)
            try:
                opened=os.fstat(fd)
                if (opened.st_dev,opened.st_ino,opened.st_size)!=(before.st_dev,before.st_ino,before.st_size) or not stat.S_ISREG(opened.st_mode):
                    raise ValueError(f"corrupt job JSON: {path.name}")
                if opened.st_size > budget:
                    raise _BatchBudgetExceeded
                read_limit=min(MAX_JOB_BYTES,budget)+1
                chunks=[]; remaining=read_limit
                while remaining and (chunk:=os.read(fd,min(65536,remaining))):
                    chunks.append(chunk); remaining-=len(chunk)
                encoded=b"".join(chunks)
            finally:
                os.close(fd)
            if len(encoded) > MAX_JOB_BYTES:
                raise ValueError(f"corrupt job JSON: {path.name}")
            if len(encoded) > budget:
                raise _BatchBudgetExceeded
            data = json.loads(encoded.decode("utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"corrupt job JSON: {path.name}") from error
        if not isinstance(data, dict):
            raise ValueError(f"corrupt job JSON: {path.name}")
        required = {"job_id", "source_path", "state", "attempts", "error", "metadata"}
        if set(data) != required:
            raise ValueError(f"corrupt job JSON: {path.name}")
        try:
            job = Job(**data)
        except (TypeError, ValueError) as error:
            raise ValueError(f"corrupt job JSON: {path.name}") from error
        self._validate_job(job, path.name)
        return self._copy(job), len(encoded)

    def _write(self, path: Path, job: Job) -> None:
        self._validate_job(job, path.name)
        payload = _bounded_json(job.to_dict())
        encoded = payload.encode("utf-8")
        temporary_name = f".{path.name}.{uuid4().hex}.tmp"
        temporary = self.root / temporary_name
        descriptor: int | None = None
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = self._open_temporary(temporary_name, flags, 0o600)
            offset = 0
            while offset < len(encoded):
                offset += os.write(descriptor, encoded[offset:])
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
            identity = (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
            )
            os.lseek(descriptor, 0, os.SEEK_SET)
            if self._read_descriptor(descriptor, len(encoded) + 1) != encoded:
                raise ValueError("queue temporary payload changed before publish")
            try:
                if os.name == "nt":
                    self._windows_replace_open_file(descriptor, path)
                else:
                    root_fd = self._require_root_descriptor()
                    os.replace(
                        temporary_name,
                        path.name,
                        src_dir_fd=root_fd,
                        dst_dir_fd=root_fd,
                    )
            except OSError:
                if not self._published_descriptor_matches(
                    descriptor, path.name, identity, encoded
                ):
                    raise
            if not self._published_descriptor_matches(
                descriptor, path.name, identity, encoded
            ):
                raise ValueError("queue publish does not match temporary payload")
            self._sync_directory()
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                if os.name == "nt":
                    temporary.unlink(missing_ok=True)
                else:
                    os.unlink(
                        temporary_name,
                        dir_fd=self._require_root_descriptor(),
                    )
            except FileNotFoundError:
                pass
            except OSError:
                pass

    @staticmethod
    def _read_descriptor(descriptor: int, limit: int) -> bytes:
        chunks: list[bytes] = []
        remaining = limit
        while remaining and (chunk := os.read(descriptor, min(65_536, remaining))):
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _published_descriptor_matches(
        self,
        descriptor: int,
        name: str,
        identity: tuple[int, int, int],
        expected: bytes,
    ) -> bool:
        try:
            info = self._entry_stat(name)
            current = (info.st_dev, info.st_ino, info.st_size)
            if current != identity or not stat.S_ISREG(info.st_mode):
                return False
            opened = os.fstat(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
            ) != identity:
                return False
            os.lseek(descriptor, 0, os.SEEK_SET)
            return self._read_descriptor(descriptor, len(expected) + 1) == expected
        except (OSError, ValueError):
            return False

    def _open_temporary(self, name: str, flags: int, mode: int) -> int:
        if os.name != "nt":
            return self._open_entry(name, flags, mode)
        import ctypes
        from ctypes import wintypes
        import msvcrt

        candidate = self.root / name
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            str(candidate),
            0x80000000 | 0x40000000 | 0x00010000,  # read, write, delete
            0x00000001 | 0x00000002,  # share read/write, never delete
            None,
            1,  # CREATE_NEW
            0x00000080 | 0x00200000,  # normal + open reparse point
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            descriptor = msvcrt.open_osfhandle(
                int(handle), os.O_RDWR | getattr(os, "O_BINARY", 0)
            )
        except BaseException:
            _windows_close_handle(int(handle))
            raise
        try:
            opened = os.fstat(descriptor)
            current = os.lstat(candidate)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or getattr(current, "st_file_attributes", 0) & reparse
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise ValueError("queue temporary entry is unsafe")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _windows_replace_open_file(descriptor: int, target: Path) -> None:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        class FileRenameInformation(ctypes.Structure):
            _fields_ = [
                ("flags", wintypes.DWORD),
                ("root_directory", wintypes.HANDLE),
                ("file_name_length", wintypes.DWORD),
                ("file_name", wintypes.WCHAR * 1),
            ]

        encoded = str(target).encode("utf-16-le")
        offset = FileRenameInformation.file_name.offset
        buffer = ctypes.create_string_buffer(
            offset + len(encoded) + ctypes.sizeof(wintypes.WCHAR)
        )
        information = ctypes.cast(
            buffer, ctypes.POINTER(FileRenameInformation)
        ).contents
        information.flags = 0x00000001  # FILE_RENAME_FLAG_REPLACE_IF_EXISTS
        information.root_directory = None
        information.file_name_length = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]
        kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        if not kernel32.SetFileInformationByHandle(
            msvcrt.get_osfhandle(descriptor), 3, buffer, len(buffer)
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def _sync_directory(self) -> None:
        if os.name == "nt":
            return
        os.fsync(self._require_root_descriptor())

    @contextmanager
    def _locked(self) -> Iterator[None]:
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = self._open_entry(self._lock_path.name, flags, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("queue lock is unsafe")
        except BaseException:
            if "descriptor" in locals():
                os.close(descriptor)
            raise
        with os.fdopen(descriptor, "a+b", buffering=0) as stream:
            if os.name == "nt":
                import msvcrt

                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                while True:
                    try:
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.01)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _require_root_descriptor(self) -> int:
        descriptor = self._root_descriptor
        if descriptor is None:
            raise RuntimeError("queue is closed")
        return descriptor

    def _entry_exists(self, name: str) -> bool:
        try:
            self._entry_stat(name)
        except FileNotFoundError:
            return False
        return True

    def _entry_stat(self, name: str):
        if os.name == "nt":
            return os.lstat(self.root / name)
        return os.stat(
            name,
            dir_fd=self._require_root_descriptor(),
            follow_symlinks=False,
        )

    def _open_entry(self, name: str, flags: int, mode: int = 0o666) -> int:
        if Path(name).name != name:
            raise ValueError("queue entry name is unsafe")
        if os.name == "nt":
            candidate = self.root / name
            before = None
            if os.path.lexists(candidate):
                before = os.lstat(candidate)
                reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                attributes = getattr(before, "st_file_attributes", 0)
                if stat.S_ISLNK(before.st_mode) or attributes & reparse:
                    label = "queue lock" if name == ".queue.lock" else "queue entry"
                    raise ValueError(f"{label} is unsafe")
            descriptor = os.open(candidate, flags, mode)
            try:
                opened = os.fstat(descriptor)
                current = os.lstat(candidate)
                reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                attributes = getattr(current, "st_file_attributes", 0)
                same_open_file = (opened.st_dev, opened.st_ino) == (
                    current.st_dev,
                    current.st_ino,
                )
                same_original = before is None or (
                    before.st_dev,
                    before.st_ino,
                ) == (current.st_dev, current.st_ino)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or not stat.S_ISREG(current.st_mode)
                    or stat.S_ISLNK(current.st_mode)
                    or attributes & reparse
                    or not same_open_file
                    or not same_original
                ):
                    label = "queue lock" if name == ".queue.lock" else "queue entry"
                    raise ValueError(f"{label} is unsafe")
                return descriptor
            except BaseException:
                os.close(descriptor)
                raise
        return os.open(
            name,
            flags,
            mode,
            dir_fd=self._require_root_descriptor(),
        )

    @staticmethod
    def _open_queue_root(path: Path) -> int:
        if os.name == "nt":
            DiskQueue._validate_windows_root_chain(path)
            path.mkdir(parents=True, exist_ok=True)
            DiskQueue._validate_windows_root_chain(path)
            try:
                return _windows_open_directory(path, path)
            except (OSError, ValueError) as error:
                raise ValueError("queue root is unsafe") from error

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        anchor = Path(path.anchor)
        descriptor = os.open(anchor, flags)
        try:
            relative_parts = path.parts[1:] if path.is_absolute() else path.parts
            for component in relative_parts:
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise ValueError("queue root is unsafe")
            return descriptor
        except BaseException as error:
            os.close(descriptor)
            if isinstance(error, ValueError):
                raise
            raise ValueError("queue root is unsafe") from error

    @staticmethod
    def _validate_windows_root_chain(path: Path) -> None:
        chain = list(reversed((path, *path.parents)))
        for component in chain:
            if not os.path.lexists(component):
                continue
            try:
                info = os.lstat(component)
            except OSError as error:
                raise ValueError("queue root is unsafe") from error
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            attributes = getattr(info, "st_file_attributes", 0)
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or attributes & reparse
                or _is_junction(component)
            ):
                raise ValueError("queue root is unsafe")

    @staticmethod
    def _copy(job: Job) -> Job:
        return Job(**json.loads(_bounded_json(job.to_dict())))

    def _validate_job(self, job: Job, filename: str) -> None:
        if (
            not isinstance(job, Job)
            or not isinstance(job.source_path, str)
            or not isinstance(job.state, str)
            or job.state not in _JOB_STATES
            or isinstance(job.attempts, bool)
            or not isinstance(job.attempts, int)
            or job.attempts < 0
            or job.error is not None and not isinstance(job.error, str)
            or not isinstance(job.metadata, dict)
        ):
            raise ValueError(f"corrupt job JSON: {filename}")
        self._validate_job_id(job.job_id)
        if filename != f"{job.job_id}.json":
            raise ValueError(f"corrupt job JSON: {filename}")
        try:
            _bounded_json(job.to_dict())
        except (TypeError, ValueError) as error:
            raise ValueError(f"corrupt job JSON: {filename}") from error
