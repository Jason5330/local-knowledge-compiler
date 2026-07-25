"""Durable, immutable storage for the raw files behind source versions."""

from contextlib import ExitStack, contextmanager
from dataclasses import asdict
import errno
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
from typing import BinaryIO, Iterator
from uuid import uuid4

from .models import SourceVersion


_CHUNK_SIZE = 1024 * 1024
_COMPONENT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")
_SOURCE_ID_RE = re.compile(r"src_[a-z0-9][a-z0-9_-]{0,127}\Z")
_VERSION_ID_RE = re.compile(r"ver_[0-9a-f]{64}\Z")
_MANIFEST_NAME = "manifest.json"
_LOCK_NAME = ".archive.lock"
_IS_WINDOWS = os.name == "nt"
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0x20000)
_POSIX_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | _O_NOFOLLOW
    | getattr(os, "O_CLOEXEC", 0)
)
_WINDOWS_FORBIDDEN_FILENAME_CHARACTERS = frozenset('<>:"|?*')
_RESERVED_WINDOWS_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def _open_posix_directory_at(parent_fd: int, component: str) -> int:
    descriptor = os.open(
        component,
        _POSIX_DIRECTORY_OPEN_FLAGS,
        dir_fd=parent_fd,
    )
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise NotADirectoryError(component)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _posix_fstat(descriptor: int):
    return os.fstat(descriptor)


def _posix_stat_at(
    component: str, *, dir_fd: int, follow_symlinks: bool
):
    return os.stat(
        component,
        dir_fd=dir_fd,
        follow_symlinks=follow_symlinks,
    )


def _verify_posix_directory_binding(
    parent_fd: int, component: str, child_fd: int
) -> None:
    try:
        pinned = _posix_fstat(child_fd)
        current = _posix_stat_at(
            component,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ValueError("source directory binding is missing") from error
    if not stat.S_ISDIR(current.st_mode):
        raise ValueError("source directory binding is not a directory")
    if (pinned.st_dev, pinned.st_ino) != (
        current.st_dev,
        current.st_ino,
    ):
        raise ValueError("source directory binding changed during archive")


def _posix_rename_noreplace(
    old_parent_fd: int,
    old_name: str,
    new_parent_fd: int,
    new_name: str,
) -> None:
    """Atomically rename without replacement, or fail closed."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    old_encoded = os.fsencode(old_name)
    new_encoded = os.fsencode(new_name)
    if sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError(
                errno.ENOSYS, "renameat2 is unavailable; refusing unsafe publish"
            )
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            old_parent_fd,
            old_encoded,
            new_parent_fd,
            new_encoded,
            1,  # RENAME_NOREPLACE
        )
    elif sys.platform == "darwin":
        rename = getattr(libc, "renameatx_np", None)
        if rename is None:
            raise OSError(
                errno.ENOSYS,
                "renameatx_np is unavailable; refusing unsafe publish",
            )
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            old_parent_fd,
            old_encoded,
            new_parent_fd,
            new_encoded,
            0x00000004,  # RENAME_EXCL
        )
    else:
        raise OSError(
            errno.ENOTSUP,
            "no atomic no-replace rename primitive for this POSIX platform",
        )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, "immutable target already exists")
    raise OSError(error_number, os.strerror(error_number))


def _windows_kernel32():
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


def _windows_close_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = _windows_kernel32()
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not kernel32.CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_open_directory(
    path: Path, raw_root: Path, *, allow_handle_rename: bool = False
) -> int:
    """Open and validate a directory without allowing delete/rename sharing."""
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = _windows_kernel32()
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD

    file_share_read = 0x00000001
    file_share_write = 0x00000002
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    generic_write = 0x40000000
    delete_access = 0x00010000
    handle = kernel32.CreateFileW(
        str(path),
        generic_write | (delete_access if allow_handle_rename else 0),
        file_share_read | file_share_write,
        None,
        open_existing,
        backup_semantics | open_reparse_point,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = ByHandleFileInformation()
        if not kernel32.GetFileInformationByHandle(
            handle, ctypes.byref(information)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        file_attribute_directory = 0x00000010
        file_attribute_reparse_point = 0x00000400
        if not information.file_attributes & file_attribute_directory:
            raise ValueError("raw store path is not a directory")
        if information.file_attributes & file_attribute_reparse_point:
            raise ValueError("raw store path is a reparse point")

        required = kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if required == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = kernel32.GetFinalPathNameByHandleW(
            handle, buffer, len(buffer), 0
        )
        if written == 0 or written >= len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        final_path = buffer.value
        if final_path.startswith("\\\\?\\UNC\\"):
            final_path = "\\\\" + final_path[8:]
        elif final_path.startswith("\\\\?\\"):
            final_path = final_path[4:]
        root_text = os.path.normcase(str(raw_root))
        final_text = os.path.normcase(str(Path(final_path)))
        if os.path.commonpath((root_text, final_text)) != root_text:
            raise ValueError("directory handle resolves outside raw_root")
        return int(handle)
    except BaseException:
        _windows_close_handle(int(handle))
        raise


def _windows_rename_directory_handle(
    source_handle: int, target: Path
) -> None:
    """Atomically rename an open directory handle to an absolute contained path."""
    import ctypes
    from ctypes import wintypes

    class FileRenameInformation(ctypes.Structure):
        _fields_ = [
            ("flags", wintypes.DWORD),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.DWORD),
            ("file_name", wintypes.WCHAR * 1),
        ]

    encoded_name = str(target).encode("utf-16-le")
    file_name_offset = FileRenameInformation.file_name.offset
    buffer = ctypes.create_string_buffer(
        file_name_offset + len(encoded_name) + ctypes.sizeof(wintypes.WCHAR)
    )
    information = ctypes.cast(
        buffer, ctypes.POINTER(FileRenameInformation)
    ).contents
    information.flags = 0
    information.root_directory = None
    information.file_name_length = len(encoded_name)
    ctypes.memmove(
        ctypes.addressof(buffer) + file_name_offset,
        encoded_name,
        len(encoded_name),
    )

    kernel32 = _windows_kernel32()
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    file_rename_info = 3
    if not kernel32.SetFileInformationByHandle(
        source_handle,
        file_rename_info,
        buffer,
        len(buffer),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    _windows_flush_handle(source_handle)


def _windows_flush_handle(handle: int) -> None:
    """Flush a writable Windows file/directory handle with bounded fallbacks."""
    import ctypes
    from ctypes import wintypes

    kernel32 = _windows_kernel32()
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
    if kernel32.FlushFileBuffers(handle):
        return
    error_code = ctypes.get_last_error()
    explicitly_unsupported = {
        6,  # ERROR_INVALID_HANDLE on filesystems without directory flush
        50,  # ERROR_NOT_SUPPORTED
        87,  # ERROR_INVALID_PARAMETER
    }
    if error_code not in explicitly_unsupported:
        raise ctypes.WinError(error_code)


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
        created = not candidate.exists()
        candidate.mkdir(parents=True, exist_ok=True)
        self.raw_root = candidate.resolve()
        if created:
            self._sync_directory(self.raw_root)
            self._sync_directory(self.raw_root.parent)

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

        with self._archive_lock(), self._pin_directory(
            self.raw_root
        ) as root_handle:
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
            self._publish(incoming_path, source, root_handle=root_handle)
            return source

    def _publish(
        self, incoming: Path, source: SourceVersion, *, root_handle: int
    ) -> None:
        if os.name != "nt":
            self._publish_posix(incoming, source, root_handle)
            return
        with ExitStack() as pins:
            parent = self._safe_directory(self.raw_root / source.space)
            pins.enter_context(self._pin_directory(parent))
            parent = self._safe_directory(parent / source.source_id)
            parent_handle = pins.enter_context(self._pin_directory(parent))
            target = parent / source.version_id
            self._ensure_contained(target)
            if os.path.lexists(target):
                raise FileExistsError("immutable target already exists")

            stage = parent / f".{source.version_id}.tmp-{uuid4().hex}"
            self._create_new_directory(stage)
            try:
                with self._pin_directory(
                    stage, allow_handle_rename=True
                ) as stage_handle:
                    copied = stage / source.original_name
                    self._copy_with_fsync(incoming, copied)
                    if file_sha256(copied) != source.sha256:
                        raise ValueError("copied file checksum does not match source")
                    self._write_manifest(stage / _MANIFEST_NAME, source)
                    self._sync_pinned_directory(stage_handle)
                    if os.path.lexists(target):
                        raise FileExistsError("immutable target already exists")
                    self._atomic_publish(
                        stage,
                        target,
                        source_handle=stage_handle,
                        parent_handle=parent_handle,
                    )
                self._sync_directory(parent)
            except BaseException:
                if stage.exists():
                    shutil.rmtree(stage)
                raise

    def _publish_posix(
        self, incoming: Path, source: SourceVersion, root_fd: int
    ) -> None:
        space_fd = self._open_or_create_posix_directory(
            root_fd, source.space
        )
        try:
            source_fd = self._open_or_create_posix_directory(
                space_fd, source.source_id
            )
            try:
                try:
                    os.stat(
                        source.version_id,
                        dir_fd=source_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise FileExistsError("immutable target already exists")

                stage_name = f".{source.version_id}.tmp-{uuid4().hex}"
                os.mkdir(stage_name, mode=0o755, dir_fd=source_fd)
                stage_fd: int | None = None
                published = False
                try:
                    stage_fd = _open_posix_directory_at(
                        source_fd, stage_name
                    )
                    self._sync_pinned_directory(stage_fd)
                    self._sync_pinned_directory(source_fd)
                    self._copy_posix_file(
                        incoming,
                        stage_fd,
                        source.original_name,
                        source.sha256,
                    )
                    self._write_posix_manifest(stage_fd, source)
                    self._sync_pinned_directory(stage_fd)
                    _verify_posix_directory_binding(
                        space_fd,
                        source.source_id,
                        source_fd,
                    )
                    _posix_rename_noreplace(
                        source_fd,
                        stage_name,
                        source_fd,
                        source.version_id,
                    )
                    published = True
                    self._sync_pinned_directory(source_fd)
                    _verify_posix_directory_binding(
                        space_fd,
                        source.source_id,
                        source_fd,
                    )
                except BaseException as archive_error:
                    if stage_fd is not None and not published:
                        try:
                            removed = self._cleanup_posix_stage(
                                stage_fd,
                                source_fd,
                                stage_name,
                                (source.original_name, _MANIFEST_NAME),
                            )
                        except BaseException as cleanup_error:
                            archive_error.add_note(
                                f"stage cleanup failed safely: {cleanup_error}"
                            )
                        else:
                            if not removed:
                                archive_error.add_note(
                                    "stage namespace changed; original files "
                                    "were unlinked by fd but orphan directory "
                                    "was preserved"
                                )
                    raise
                finally:
                    if stage_fd is not None:
                        os.close(stage_fd)
            finally:
                os.close(source_fd)
        finally:
            os.close(space_fd)

    def _open_or_create_posix_directory(
        self, parent_fd: int, component: str
    ) -> int:
        try:
            return _open_posix_directory_at(parent_fd, component)
        except FileNotFoundError:
            os.mkdir(component, mode=0o755, dir_fd=parent_fd)
            try:
                descriptor = _open_posix_directory_at(
                    parent_fd, component
                )
            except BaseException:
                try:
                    os.rmdir(component, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                raise
            try:
                self._sync_pinned_directory(descriptor)
                self._sync_pinned_directory(parent_fd)
            except BaseException:
                os.close(descriptor)
                try:
                    os.rmdir(component, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                raise
            return descriptor

    def _copy_posix_file(
        self,
        incoming: Path,
        stage_fd: int,
        original_name: str,
        expected_digest: str,
    ) -> None:
        write_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | _O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        output_fd = os.open(
            original_name,
            write_flags,
            0o600,
            dir_fd=stage_fd,
        )
        try:
            with incoming.open("rb") as input_file, os.fdopen(
                output_fd, "wb", closefd=False
            ) as output_file:
                while chunk := input_file.read(_CHUNK_SIZE):
                    output_file.write(chunk)
                self._flush_file(output_file)
        finally:
            os.close(output_fd)

        read_fd = os.open(
            original_name,
            os.O_RDONLY | _O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=stage_fd,
        )
        try:
            if self._descriptor_sha256(read_fd) != expected_digest:
                raise ValueError("copied file checksum does not match source")
        finally:
            os.close(read_fd)

    def _write_posix_manifest(
        self, stage_fd: int, source: SourceVersion
    ) -> None:
        encoded = (
            json.dumps(
                asdict(source), sort_keys=True, ensure_ascii=False
            ).encode("utf-8")
            + b"\n"
        )
        descriptor = os.open(
            _MANIFEST_NAME,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | _O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=stage_fd,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as manifest:
                manifest.write(encoded)
                self._flush_file(manifest)
        finally:
            os.close(descriptor)

    @staticmethod
    def _descriptor_sha256(descriptor: int) -> str:
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, _CHUNK_SIZE):
            digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _cleanup_posix_stage(
        stage_fd: int,
        parent_fd: int,
        stage_name: str,
        owned_names: tuple[str, ...],
    ) -> bool:
        for name in owned_names:
            try:
                os.unlink(name, dir_fd=stage_fd)
            except FileNotFoundError:
                pass
        original = _posix_fstat(stage_fd)
        try:
            current = _posix_stat_at(
                stage_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError:
            return False
        if not stat.S_ISDIR(current.st_mode):
            return False
        if (original.st_dev, original.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            return False
        os.rmdir(stage_name, dir_fd=parent_fd)
        return True

    @contextmanager
    def _archive_lock(self) -> Iterator[None]:
        """Serialize archives with a process-owned kernel lock."""
        lock = self.raw_root / _LOCK_NAME
        self._ensure_contained(lock)
        if os.path.lexists(lock) and (
            lock.is_symlink() or _is_junction(lock) or lock.is_dir()
        ):
            raise ValueError("archive lock is unsafe")
        lock_file = lock.open("a+b", buffering=0)
        acquired = False
        deadline = time.monotonic() + 30
        try:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                self._flush_file(lock_file)
            while True:
                try:
                    self._try_lock_file(lock_file)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "timed out waiting for source archive lock"
                        )
                    time.sleep(0.01)
            yield
        finally:
            try:
                if acquired:
                    self._unlock_file(lock_file)
            finally:
                lock_file.close()

    @staticmethod
    def _try_lock_file(lock_file: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EDEADLK}:
                    raise BlockingIOError from error
                raise
            return
        import fcntl

        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise BlockingIOError from error
            raise

    @staticmethod
    def _unlock_file(lock_file: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

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

    def _copy_with_fsync(self, source: Path, destination: Path) -> None:
        with source.open("rb") as input_file, destination.open("xb") as output_file:
            while chunk := input_file.read(_CHUNK_SIZE):
                output_file.write(chunk)
            self._flush_file(output_file)

    def _write_manifest(self, path: Path, source: SourceVersion) -> None:
        encoded = json.dumps(asdict(source), sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )
        with path.open("xb") as manifest:
            manifest.write(encoded)
            manifest.write(b"\n")
            self._flush_file(manifest)

    @staticmethod
    def _flush_file(file_object: BinaryIO) -> None:
        file_object.flush()
        os.fsync(file_object.fileno())

    def _sync_directory(self, path: Path) -> None:
        if os.name == "nt":
            validation_root = self.raw_root
            if path.resolve() == self.raw_root.parent:
                validation_root = path.resolve()
            handle = _windows_open_directory(path, validation_root)
            try:
                _windows_flush_handle(handle)
            finally:
                _windows_close_handle(handle)
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            try:
                os.fsync(descriptor)
            except OSError as error:
                unsupported = {
                    errno.EBADF,
                    errno.EINVAL,
                    getattr(errno, "ENOTSUP", errno.EINVAL),
                    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
                }
                if error.errno not in unsupported:
                    raise
        finally:
            os.close(descriptor)

    @staticmethod
    def _sync_pinned_directory(handle: int) -> None:
        if os.name == "nt":
            _windows_flush_handle(handle)
            return
        try:
            os.fsync(handle)
        except OSError as error:
            unsupported = {
                errno.EBADF,
                errno.EINVAL,
                getattr(errno, "ENOTSUP", errno.EINVAL),
                getattr(errno, "EOPNOTSUPP", errno.EINVAL),
            }
            if error.errno not in unsupported:
                raise

    @contextmanager
    def _pin_directory(
        self, path: Path, *, allow_handle_rename: bool = False
    ) -> Iterator[int]:
        self._ensure_contained(path)
        if os.name == "nt":
            handle = _windows_open_directory(
                path,
                self.raw_root,
                allow_handle_rename=allow_handle_rename,
            )
            try:
                yield handle
            finally:
                _windows_close_handle(handle)
            return

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise ValueError("raw store path is not a directory")
            proc_handle = Path("/proc/self/fd") / str(descriptor)
            if proc_handle.exists():
                try:
                    proc_handle.resolve().relative_to(self.raw_root)
                except ValueError as error:
                    raise ValueError(
                        "directory handle resolves outside raw_root"
                    ) from error
            yield descriptor
        finally:
            os.close(descriptor)

    def _create_new_directory(self, path: Path) -> None:
        self._ensure_contained(path)
        path.mkdir()
        self._ensure_contained(path)
        self._sync_directory(path)
        self._sync_directory(path.parent)

    def _atomic_publish(
        self,
        source: Path,
        target: Path,
        *,
        source_handle: int,
        parent_handle: int,
    ) -> None:
        self._ensure_contained(source)
        self._ensure_contained(target)
        if os.path.lexists(target):
            raise FileExistsError("immutable target already exists")
        if _IS_WINDOWS:
            _windows_rename_directory_handle(source_handle, target)
            return
        _posix_rename_noreplace(
            parent_handle,
            source.name,
            parent_handle,
            target.name,
        )

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
        created = False
        if os.path.lexists(path):
            if path.is_symlink() or _is_junction(path) or not path.is_dir():
                raise ValueError("raw store path is unsafe")
        else:
            path.mkdir()
            created = True
        self._ensure_contained(path)
        if created:
            self._sync_directory(path)
            self._sync_directory(path.parent)
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
