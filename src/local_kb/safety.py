"""Pinned filesystem helpers for vault and SQLite paths."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
from typing import Iterator


CATALOG_SUFFIXES = ("", "-wal", "-shm", "-journal")


def is_reparse(path: Path) -> bool:
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def secure_directory(path: Path | str) -> Path:
    """Create a directory from the filesystem anchor without traversing links."""
    target = Path(os.path.abspath(os.fspath(path)))
    if os.name == "nt":
        return _secure_windows_directory(target)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        descriptor = os.open(target.anchor, flags)
        descriptors.append(descriptor)
        for component in target.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                raise ValueError("directory path is unsafe")
            descriptors.append(child)
            descriptor = child
    except OSError as error:
        raise ValueError("directory path contains an unsafe link") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return target


def _secure_windows_directory(target: Path) -> Path:
    from .source_store import _is_junction, _windows_close_handle

    handles: list[int] = []
    anchor = Path(target.anchor)
    cursor = anchor
    try:
        handles.append(_open_windows_directory_readonly(cursor))
        for component in target.parts[1:]:
            cursor = cursor / component
            if os.path.lexists(cursor):
                info = os.lstat(cursor)
                if (
                    stat.S_ISLNK(info.st_mode)
                    or not stat.S_ISDIR(info.st_mode)
                    or _is_junction(cursor)
                    or is_reparse(cursor)
                ):
                    raise ValueError("directory path contains an unsafe link or reparse point")
            else:
                try:
                    os.mkdir(cursor)
                except FileExistsError:
                    pass
            handles.append(_open_windows_directory_readonly(cursor))
    except OSError as error:
        raise ValueError("directory path contains an unsafe link or reparse point") from error
    finally:
        for handle in reversed(handles):
            _windows_close_handle(handle)
    return target


def _open_windows_directory_readonly(path: Path) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    file_read_attributes = 0x00000080
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    handle = kernel32.CreateFileW(
        str(path),
        file_read_attributes,
        file_share_read | file_share_write,
        None,
        open_existing,
        backup_semantics | open_reparse_point,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or is_reparse(path):
        from .source_store import _windows_close_handle

        _windows_close_handle(handle)
        raise ValueError("directory path contains an unsafe reparse point")
    return int(handle)


def _catalog_info(path: Path, parent_fd: int | None) -> os.stat_result | None:
    try:
        info = (
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if parent_fd is not None
            else os.lstat(path)
        )
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or is_reparse(path)
        or info.st_nlink != 1
    ):
        raise ValueError(f"catalog path is unsafe or not single-link: {path.name}")
    return info


def _pin_catalog_entry(
    path: Path, parent_fd: int | None, expected: os.stat_result
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = (
        os.open(path.name, flags, dir_fd=parent_fd)
        if parent_fd is not None
        else os.open(path, flags)
    )
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        os.close(descriptor)
        raise ValueError(f"catalog binding changed while opening: {path.name}")
    return descriptor


@contextmanager
def guarded_catalog_path(
    path: Path | str, *, allow_missing_main: bool = False
) -> Iterator[Path]:
    """Pin the catalog parent and validate SQLite's main and sidecar files."""
    from .compiler import ManualCompiler

    database = Path(os.path.abspath(os.fspath(path)))
    parent = database.parent
    secure_directory(parent)
    locker = ManualCompiler(parent, trusted_root=parent)
    with locker._pinned_outbox(create=False) as parent_fd:
        baseline: dict[str, tuple[int, int]] = {}
        descriptors: dict[str, int] = {}
        try:
            for suffix in CATALOG_SUFFIXES:
                candidate = Path(f"{database}{suffix}")
                info = _catalog_info(candidate, parent_fd)
                if info is None:
                    if not suffix and not allow_missing_main:
                        raise FileNotFoundError(database)
                    continue
                descriptors[suffix] = _pin_catalog_entry(candidate, parent_fd, info)
                baseline[suffix] = (info.st_dev, info.st_ino)
            if parent_fd is not None and Path("/proc/self/fd").is_dir():
                bound = Path(f"/proc/self/fd/{parent_fd}/{database.name}")
            else:
                bound = database
            yield bound
        finally:
            try:
                for suffix in CATALOG_SUFFIXES:
                    candidate = Path(f"{database}{suffix}")
                    info = _catalog_info(candidate, parent_fd)
                    if info is None:
                        if suffix == "" and suffix in baseline:
                            raise ValueError("catalog binding disappeared")
                        continue
                    previous = baseline.get(suffix)
                    if previous is not None and previous != (info.st_dev, info.st_ino):
                        raise ValueError(f"catalog binding changed: {candidate.name}")
                    descriptor = descriptors.get(suffix)
                    if descriptor is not None:
                        opened = os.fstat(descriptor)
                        if previous != (opened.st_dev, opened.st_ino):
                            raise ValueError(f"catalog pinned binding changed: {candidate.name}")
            finally:
                for descriptor in descriptors.values():
                    os.close(descriptor)
