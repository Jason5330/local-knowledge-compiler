"""Contracts and dispatch for local, non-executing document extractors."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
from typing import Callable, Protocol


MAX_SOURCE_BYTES = 100 * 1024 * 1024
MAX_ZIP_MEMBERS = 2_000
MAX_ZIP_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_ZIP_SINGLE_XML_BYTES = 25 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 200
ZIP_RATIO_MIN_UNCOMPRESSED_BYTES = 10 * 1024 * 1024
MAX_WORKSHEET_ROWS = 100_000
MAX_WORKSHEET_COLUMNS = 256
MAX_WORKSHEET_CELLS = 1_000_000
MAX_FRAGMENT_COUNT = 20_000
MAX_EXTRACTION_CHARACTERS = 2_000_000


@dataclass(frozen=True)
class Fragment:
    locator: str
    text: str


@dataclass(frozen=True)
class Extraction:
    status: str
    fragments: list[Fragment]
    warning: str | None = None


class ExtractionError(RuntimeError):
    """A supported local document could not safely be read."""


def enforce_extraction_budget(extraction: Extraction) -> Extraction:
    """Reject extractor output that exceeds the shared evidence budget."""
    if len(extraction.fragments) > MAX_FRAGMENT_COUNT:
        raise ExtractionError(
            f"extraction fragment count exceeds budget of {MAX_FRAGMENT_COUNT}"
        )
    characters = 0
    for fragment in extraction.fragments:
        characters += len(fragment.text)
        if characters > MAX_EXTRACTION_CHARACTERS:
            raise ExtractionError(
                "extraction character count exceeds budget of "
                f"{MAX_EXTRACTION_CHARACTERS}"
            )
    return extraction


class SnapshotCleanupError(RuntimeError):
    """A private parser snapshot could not be removed after use."""

    def __init__(self, snapshot_directory: Path) -> None:
        self.snapshot_directory = snapshot_directory
        super().__init__(f"failed to remove extraction snapshot: {snapshot_directory}")


class Extractor(Protocol):
    suffixes: set[str] | frozenset[str]

    def extract(self, path: Path) -> Extraction: ...


def _write_all(fd: int, data: bytes) -> None:
    """Write a complete chunk, including after a short operating-system write."""
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written == 0:
            raise OSError("short write while creating extraction snapshot")
        offset += written


def _open_posix_source(candidate: Path) -> tuple[int, list[int]]:
    """Open every POSIX component without following links, retaining the fds."""
    components = candidate.parts
    if not components or components[0] != os.sep or len(components) < 2:
        raise ValueError(f"extractor input must be an existing regular file: {candidate}")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fds = [os.open(os.sep, directory_flags)]
    try:
        for component in components[1:-1]:
            directory_fds.append(
                os.open(component, directory_flags, dir_fd=directory_fds[-1])
            )
        source_fd = os.open(
            components[-1],
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_fds[-1],
        )
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            os.close(source_fd)
            raise ValueError(f"extractor input must be an existing regular file: {candidate}")
        return source_fd, directory_fds
    except Exception:
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)
        raise


def _open_windows_source(candidate: Path) -> tuple[int, list[int]]:
    """Pin each Windows path component and open the final regular file safely."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_read = 0x80000000
    share_read_write = 0x00000001 | 0x00000002  # Deliberately excludes FILE_SHARE_DELETE.
    open_existing = 3
    flag_backup_semantics = 0x02000000
    flag_open_reparse_point = 0x00200000
    attribute_directory = 0x00000010
    attribute_reparse_point = 0x00000400
    file_type_disk = 0x0001
    invalid_handle_value = ctypes.c_void_p(-1).value

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

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
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
    get_information.restype = wintypes.BOOL
    get_file_type = kernel32.GetFileType
    get_file_type.argtypes = [wintypes.HANDLE]
    get_file_type.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    def open_component(component: str, *, directory: bool) -> tuple[int, int]:
        flags = flag_open_reparse_point | (flag_backup_semantics if directory else 0)
        handle = create_file(
            component,
            generic_read,
            share_read_write,
            None,
            open_existing,
            flags,
            None,
        )
        if handle == invalid_handle_value:
            raise ctypes.WinError(ctypes.get_last_error())
        information = FileInformation()
        if not get_information(handle, ctypes.byref(information)):
            error = ctypes.WinError(ctypes.get_last_error())
            close_handle(handle)
            raise error
        attributes = information.dwFileAttributes
        if attributes & attribute_reparse_point:
            close_handle(handle)
            raise ValueError(f"extractor input must not contain a reparse point: {component}")
        if bool(attributes & attribute_directory) != directory:
            close_handle(handle)
            expected = "directory" if directory else "regular file"
            raise ValueError(f"extractor input component is not a {expected}: {component}")
        return handle, attributes

    components = candidate.parts
    if not candidate.anchor or len(components) < 2:
        raise ValueError(f"extractor input must be an existing regular file: {candidate}")
    directory_handles: list[int] = []
    try:
        current = candidate.anchor
        root_handle, _ = open_component(current, directory=True)
        directory_handles.append(root_handle)
        for component in components[1:-1]:
            current = os.path.join(current, component)
            handle, _ = open_component(current, directory=True)
            directory_handles.append(handle)
        final_path = os.path.join(current, components[-1])
        final_handle, _ = open_component(final_path, directory=False)
        if get_file_type(final_handle) != file_type_disk:
            close_handle(final_handle)
            raise ValueError(f"extractor input must be a disk regular file: {candidate}")
        try:
            source_fd = msvcrt.open_osfhandle(final_handle, os.O_RDONLY | os.O_BINARY)
        except Exception:
            close_handle(final_handle)
            raise
        return source_fd, directory_handles
    except Exception:
        for directory_handle in reversed(directory_handles):
            close_handle(directory_handle)
        raise


def _open_safe_source(path: Path) -> tuple[Path, int, list[int], Callable[[list[int]], None]]:
    candidate = Path(os.path.abspath(os.fspath(path)))
    try:
        if os.name == "nt":
            source_fd, directory_handles = _open_windows_source(candidate)
            return candidate, source_fd, directory_handles, _close_windows_handles
        source_fd, directory_handles = _open_posix_source(candidate)
        return candidate, source_fd, directory_handles, _close_posix_fds
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"extractor input must be a safe existing regular file: {candidate}") from exc


def _close_source(
    source_fd: int,
    directory_handles: list[int],
    close_directory: Callable[[list[int]], None],
) -> None:
    try:
        os.close(source_fd)
    finally:
        close_directory(directory_handles)


def validate_regular_file(path: Path) -> None:
    """Safely validate an unhandled file without copying its contents."""
    _, source_fd, directory_handles, close_directory = _open_safe_source(path)
    _close_source(source_fd, directory_handles, close_directory)


def _cleanup_snapshot_directory(snapshot_directory: Path) -> None:
    """Remove only this extraction's private temporary directory, or fail loudly."""
    failure: OSError | None = None
    for attempt in range(3):
        try:
            shutil.rmtree(snapshot_directory)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            failure = exc
            if not snapshot_directory.exists():
                return
            if attempt < 2:
                time.sleep(0.02)
    assert failure is not None
    raise SnapshotCleanupError(snapshot_directory) from failure


@contextmanager
def snapshot_file(path: Path):
    """Copy a pinned, regular local source to a private immutable parser input."""
    candidate, source_fd, directory_handles, close_directory = _open_safe_source(path)
    try:
        source_size = os.fstat(source_fd).st_size
    except Exception:
        _close_source(source_fd, directory_handles, close_directory)
        raise
    if source_size > MAX_SOURCE_BYTES:
        _close_source(source_fd, directory_handles, close_directory)
        raise ExtractionError(
            f"supported source exceeds 100 MiB budget: {candidate}"
        )
    snapshot_directory: Path | None = None
    destination_fd: int | None = None
    try:
        snapshot_directory = Path(tempfile.mkdtemp(prefix="local-kb-extract-"))
        snapshot_path = snapshot_directory / candidate.name
        destination_fd = os.open(
            snapshot_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        while chunk := os.read(source_fd, 1024 * 1024):
            _write_all(destination_fd, chunk)
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = None
        yield snapshot_path
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        try:
            _close_source(source_fd, directory_handles, close_directory)
        finally:
            if snapshot_directory is not None:
                _cleanup_snapshot_directory(snapshot_directory)


def _close_posix_fds(file_descriptors: list[int]) -> None:
    for file_descriptor in reversed(file_descriptors):
        os.close(file_descriptor)


def _close_windows_handles(handles: list[int]) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    for handle in reversed(handles):
        close_handle(handle)


class Registry:
    def __init__(self) -> None:
        self._items: dict[str, Extractor] = {}

    def register(self, extractor: Extractor) -> None:
        suffixes = {suffix.lower() for suffix in extractor.suffixes}
        duplicates = sorted(suffix for suffix in suffixes if suffix in self._items)
        if duplicates:
            raise ValueError(f"duplicate extractor suffix: {duplicates[0]}")
        self._items.update({suffix: extractor for suffix in suffixes})

    def extract(self, path: Path) -> Extraction:
        suffix = Path(path).suffix.lower()
        extractor = self._items.get(suffix)
        if extractor is not None:
            with snapshot_file(path) as snapshot:
                extract_snapshot = getattr(extractor, "extract_snapshot", None)
                if extract_snapshot is not None:
                    return enforce_extraction_budget(extract_snapshot(snapshot))
                return enforce_extraction_budget(extractor.extract(snapshot))
        from .unsupported import pending_extractor

        validate_regular_file(path)
        return pending_extractor(Path(path))


registry = Registry()
