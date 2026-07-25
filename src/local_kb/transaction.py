"""Secure all-or-nothing publication and narrowly-scoped Git commits."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
from typing import Callable, Iterator


_MANAGED_ROOTS = ("20_wiki", "30_answers", "40_index/index.md", "90_logs")
_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})
_LOCK_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


class RollbackError(RuntimeError):
    """Rollback could not completely restore an externally visible transaction."""
    def __init__(self, original: BaseException, errors: list[BaseException]) -> None:
        self.original = original
        self.errors = tuple(errors)
        super().__init__(f"rollback incomplete after {original}: " + "; ".join(str(error) for error in errors))


def _is_reparse(path: Path) -> bool:
    try:
        attrs = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError):
        return path.is_symlink()
    return path.is_symlink() or bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _fsync_file(path: Path) -> None:
    # Windows rejects FlushFileBuffers for a read-only descriptor.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ChangeTransaction:
    """A private staging transaction for the vault's generated files only."""

    def __init__(self, vault: str | Path) -> None:
        self.vault = Path(vault).resolve(strict=False)
        if not self.vault.exists() or not self.vault.is_dir() or _is_reparse(self.vault):
            raise ValueError("vault must be an existing non-reparse directory")
        self.transaction_id = uuid.uuid4().hex
        self.stage_root = self.vault / ".kb" / "staging" / self.transaction_id
        self._staged: dict[str, Path] = {}
        self._created_live_dirs: list[Path] = []
        self.cleanup_warning: str | None = None

    def _relative(self, relative_path: str | Path) -> str:
        raw = str(relative_path)
        if not raw or "\\" in raw or "\x00" in raw or any(ord(ch) < 32 for ch in raw):
            raise ValueError("path must be a canonical relative POSIX path")
        if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
            raise ValueError("path must be relative")
        pure = PurePosixPath(raw)
        if str(pure) != raw or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise ValueError("path traversal or non-canonical path")
        for part in pure.parts:
            stem = part.rstrip(". ").split(".", 1)[0].upper()
            if part != part.rstrip(". ") or ":" in part or stem in _RESERVED:
                raise ValueError("path uses an unsafe Windows name")
        normalized = "/".join(pure.parts)
        if not self._managed(normalized):
            raise ValueError("path is outside managed vault roots")
        return normalized

    @staticmethod
    def _managed(relative: str) -> bool:
        return (relative.startswith("20_wiki/") or relative.startswith("30_answers/")
                or relative.startswith("90_logs/") or relative == "40_index/index.md")

    def _safe_parents(self, path: Path, *, create: bool) -> None:
        try:
            relative = path.relative_to(self.vault)
        except ValueError as exc:
            raise ValueError("path escapes vault") from exc
        cursor = self.vault
        if _is_reparse(cursor):
            raise ValueError("vault is a symlink or reparse point")
        for part in relative.parts[:-1]:
            cursor = cursor / part
            if cursor.exists() or cursor.is_symlink():
                if _is_reparse(cursor) or not cursor.is_dir():
                    raise ValueError("path parent is a symlink, reparse point, or non-directory")
            elif create:
                cursor.mkdir()
                if not str(cursor).startswith(str(self.stage_root)):
                    self._created_live_dirs.append(cursor)
        # Resolve is a final defense against a directory unexpectedly redirecting us.
        parent = path.parent.resolve(strict=False)
        try:
            parent.relative_to(self.vault.resolve())
        except ValueError as exc:
            raise ValueError("path escapes vault") from exc

    def _case_safe(self, path: Path) -> None:
        """Reject a spelling that aliases a sibling on case-insensitive vaults."""
        cursor = self.vault
        relative = path.relative_to(self.vault)
        for part in relative.parts:
            if cursor.exists():
                matches = [entry.name for entry in cursor.iterdir() if entry.name.casefold() == part.casefold()]
                if matches and part not in matches:
                    raise ValueError("path case aliases an existing live entry")
            cursor = cursor / part

    def _atomic_write(self, destination: Path, content: str) -> None:
        self._safe_parents(destination, create=True)
        if destination.exists() or destination.is_symlink():
            raise ValueError("staging destination already exists")
        fd, name = tempfile.mkstemp(prefix=".kb-", suffix=".tmp", dir=destination.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            _fsync_dir(destination.parent)
        finally:
            if temporary.exists():
                temporary.unlink()

    def stage(self, relative_path: str | Path, content: str) -> Path:
        """Atomically add one UTF-8 generated file to this transaction's staging tree."""
        if not isinstance(content, str):
            raise ValueError("content must be text")
        relative = self._relative(relative_path)
        key = relative.casefold()
        if key in self._staged:
            raise ValueError("duplicate staged path (case-insensitive)")
        destination = self.stage_root / Path(*PurePosixPath(relative).parts)
        self._safe_parents(self.vault / Path(*PurePosixPath(relative).parts), create=False)
        self._atomic_write(destination, content)
        self._staged[key] = destination
        return destination

    @contextmanager
    def _writer_lock(self) -> Iterator[None]:
        runtime = self.vault / ".kb"
        if runtime.exists() and (_is_reparse(runtime) or not runtime.is_dir()):
            raise ValueError(".kb must not be a symlink or reparse point")
        runtime.mkdir(exist_ok=True)
        lock_path = runtime / "write.lock"
        lock_key = str(lock_path.resolve(strict=False)).casefold()
        with _LOCK_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(lock_key, threading.Lock())
        with thread_lock:
            created = False
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(lock_path, flags, 0o600)
                created = True
            except FileExistsError:
                info = lock_path.lstat()
                if _is_reparse(lock_path) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("write.lock must be a single-link regular file")
                fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "r+b", closefd=True) as handle:
                if created:
                    handle.write(b"0")
                    handle.flush()
                    os.fsync(handle.fileno())
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                try:
                    yield
                finally:
                    unlock()

    def _staged_paths(self) -> tuple[tuple[str, Path], ...]:
        return tuple(sorted(((path.relative_to(self.stage_root).as_posix(), path)
                             for path in self._staged.values()), key=lambda item: item[0]))

    def _write_live_temp(self, target: Path, staged: Path) -> Path:
        self._safe_parents(target, create=True)
        if target.exists() and (_is_reparse(target) or not target.is_file()):
            raise ValueError("live target must be a regular file")
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".new", dir=target.parent)
        temporary = Path(name)
        try:
            with staged.open("rb") as source, os.fdopen(fd, "wb") as output:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            return temporary
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def publish(self, validator: Callable[[tuple[Path, ...]], object]) -> None:
        """Validate then atomically publish all staged files, or restore every live file."""
        if not callable(validator):
            raise ValueError("validator must be callable")
        entries = self._staged_paths()
        with self._writer_lock():
            # Validation is deliberately before *any* live directory or file mutation.
            validator(tuple(staged for _, staged in entries))
            prepared: list[tuple[Path, Path, bytes | None, os.stat_result | None]] = []
            try:
                for relative, staged in entries:
                    target = self.vault / Path(*PurePosixPath(relative).parts)
                    self._case_safe(target)
                    self._safe_parents(target, create=False)
                    old_stat = target.stat() if target.exists() else None
                    old_bytes = target.read_bytes() if old_stat is not None else None
                    prepared.append((target, self._write_live_temp(target, staged), old_bytes, old_stat))
                # Commit point: the private stage must be gone before any live
                # replacement; backups are already retained in memory.
                stage_error: OSError | None = None
                for _ in range(2):
                    try:
                        shutil.rmtree(self.stage_root, ignore_errors=False)
                        stage_error = None
                        break
                    except OSError as exc:
                        stage_error = exc
                if stage_error is not None:
                    self._cleanup_created_dirs()
                    raise stage_error
                published: list[tuple[Path, bytes | None, os.stat_result | None]] = []
                try:
                    for target, temporary, old_bytes, old_stat in prepared:
                        os.replace(temporary, target)
                        # A successful replace is externally visible even if the
                        # following durability check fails, so it must be tracked
                        # before fsync in order to be rolled back.
                        published.append((target, old_bytes, old_stat))
                        _fsync_file(target)
                        _fsync_dir(target.parent)
                except BaseException as original:
                    rollback_errors: list[BaseException] = []
                    for target, old_bytes, old_stat in reversed(published):
                        for attempt in range(2):
                            try:
                                if old_bytes is None:
                                    try:
                                        target.unlink()
                                    except FileNotFoundError:
                                        pass
                                else:
                                    self._restore(target, old_bytes, old_stat)
                                break
                            except BaseException as exc:
                                if attempt:
                                    rollback_errors.append(exc)
                    self._cleanup_created_dirs()
                    if rollback_errors:
                        raise RollbackError(original, rollback_errors) from original
                    raise
            except BaseException:
                self._cleanup_created_dirs()
                raise
            finally:
                for _, temporary, _, _ in prepared:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
            self._staged.clear()

    def _cleanup_created_dirs(self) -> None:
        for directory in reversed(self._created_live_dirs):
            try:
                if directory.exists() and not _is_reparse(directory):
                    directory.rmdir()
            except OSError:
                pass
        self._created_live_dirs.clear()

    def _restore(self, target: Path, data: bytes, metadata: os.stat_result | None) -> None:
        temporary: Path | None = None
        replaced = False
        try:
            temporary = self._write_live_temp_from_bytes(target, data)
            for _ in range(2):
                try:
                    os.replace(temporary, target)
                    replaced = True
                    break
                except OSError:
                    continue
            if not replaced:
                # Last-resort recovery when atomic rename itself is unavailable.
                # The target is already lock- and path-validated by publish.
                with target.open("w+b") as output:
                    output.write(data)
                    output.truncate()
                    output.flush()
                    os.fsync(output.fileno())
            if metadata is not None:
                os.chmod(target, stat.S_IMODE(metadata.st_mode))
                os.utime(target, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            _fsync_file(target)
            _fsync_dir(target.parent)
            with target.open("rb") as restored:
                if restored.read() != data:
                    raise OSError("rollback verification failed")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def _write_live_temp_from_bytes(self, target: Path, data: bytes) -> Path:
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".new", dir=target.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        return temporary

    def commit_git(self, message: str) -> bool:
        """Commit generated roots only, without absorbing a user's staged files."""
        if (not isinstance(message, str) or not message.strip() or message != message.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in message)):
            raise ValueError("commit message must be a single non-empty safe line")
        try:
            inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=self.vault,
                                    text=True, capture_output=True, check=False).returncode == 0
            if not inside:
                subprocess.run(["git", "init"], cwd=self.vault, text=True, capture_output=True, check=True)
            pathspecs = [path for path in _MANAGED_ROOTS if (self.vault / path).exists()]
            tracked = subprocess.run(["git", "ls-files", "-z", "--", *_MANAGED_ROOTS], cwd=self.vault,
                                     text=False, capture_output=True, check=True).stdout.split(b"\0")
            pathspecs.extend(path.decode("utf-8", "surrogateescape") for path in tracked if path)
            pathspecs = list(dict.fromkeys(pathspecs))
            if not pathspecs:
                return False
            subprocess.run(["git", "add", "-A", "--", *pathspecs], cwd=self.vault,
                           text=True, capture_output=True, check=True)
            cached = subprocess.run(["git", "diff", "--cached", "--quiet", "--", *pathspecs], cwd=self.vault,
                                    text=True, capture_output=True, check=False)
            if cached.returncode == 0:
                return False
            if cached.returncode != 1:
                raise RuntimeError(cached.stderr or "unable to inspect managed Git changes")
            changed = subprocess.run(["git", "diff", "--cached", "--no-renames", "--name-only", "-z", "--", *pathspecs], cwd=self.vault,
                                     text=False, capture_output=True, check=True).stdout.split(b"\0")
            commit_paths = [path.decode("utf-8", "surrogateescape") for path in changed if path]
            # --only records the working-tree content, but Git requires untracked
            # pathspecs to be known to its index first.  Intent-to-add does not
            # stage their bytes and leaves unrelated user staging untouched.
            command = [
                "git", "-c", "user.name=Local Knowledge Compiler", "-c", "user.email=kb@local",
                "-c", "commit.gpgsign=false", "commit", "--only", "-m", message, "--", *commit_paths,
            ]
            subprocess.run(command, cwd=self.vault, text=True, capture_output=True, check=True,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            return True
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise RuntimeError(f"git commit failed: {detail}") from exc
