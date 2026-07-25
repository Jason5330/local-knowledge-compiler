"""Secure all-or-nothing publication and narrowly-scoped Git commits."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
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


class ConflictError(RuntimeError):
    """The live namespace changed after the transaction was prepared."""


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


def _fingerprint(path: Path) -> dict[str, object]:
    info = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"sha256": digest, "size": info.st_size, "mode": stat.S_IMODE(info.st_mode),
            "mtime_ns": info.st_mtime_ns}


def _write_manifest(journal: Path, manifest: dict[str, object]) -> None:
    destination = journal / "manifest.json"
    fd, name = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=journal)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            json.dump(manifest, output, sort_keys=True, separators=(",", ":"))
            output.write("\n"); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, destination)
        _fsync_dir(journal)
    finally:
        temporary.unlink(missing_ok=True)


def _recover_journal(vault: Path, journal: Path) -> None:
    manifest_path = journal / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("version") != 1 or manifest.get("state") not in {"prepared", "committed"}:
            raise ValueError("invalid journal manifest")
        entries = manifest["entries"]
        if not isinstance(entries, list):
            raise ValueError("invalid journal entries")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"corrupt transaction journal: {journal.name}") from exc
    checker = ChangeTransaction(vault)
    validated: list[tuple[dict[str, object], Path, Path, Path]] = []
    # Validate the complete manifest and every filesystem object before touching live.
    for item in entries:
        if not isinstance(item, dict):
            raise RuntimeError("corrupt transaction journal entry")
        relative = str(item.get("relative", ""))
        pure = PurePosixPath(relative)
        backup_rel = str(item.get("backup", ""))
        new_rel = str(item.get("new", ""))
        if (str(pure) != relative or pure.is_absolute() or ".." in pure.parts
                or not re.fullmatch(r"backups/[0-9]+\.bak", backup_rel)
                or not re.fullmatch(r"new/[0-9]+\.new", new_rel)
                or not isinstance(item.get("existed"), bool)
                or not isinstance(item.get("published"), bool)):
            raise RuntimeError("corrupt transaction journal entry")
        checker._relative(relative)
        target = vault / Path(*pure.parts)
        checker._safe_parents(target, create=False)
        checker._case_safe(target)
        if target.exists() or target.is_symlink():
            target_info = target.lstat()
            if _is_reparse(target) or not stat.S_ISREG(target_info.st_mode):
                raise ConflictError(f"unsafe recovery target: {relative}")
        new_path = journal / Path(*PurePosixPath(new_rel).parts)
        backup = journal / Path(*PurePosixPath(backup_rel).parts)
        if manifest["state"] == "committed":
            expected = item.get("new_fingerprint")
            if not target.exists() or not isinstance(expected, dict):
                raise ConflictError(f"committed live target missing: {relative}")
            actual = _fingerprint(target)
            if actual["sha256"] != expected.get("sha256") or actual["size"] != expected.get("size"):
                raise ConflictError(f"committed live target changed: {relative}")
            validated.append((item, target, new_path, backup))
            continue
        if not new_path.exists():
            raise RuntimeError("corrupt transaction journal new file")
        for candidate, expected, links in ((new_path, item.get("new_fingerprint"), {1, 2}),
                                           (backup, item.get("base"), {1})):
            if candidate.exists():
                info = candidate.lstat()
                if (_is_reparse(candidate) or not stat.S_ISREG(info.st_mode) or info.st_nlink not in links
                        or not isinstance(expected, dict) or _fingerprint(candidate) != expected):
                    raise RuntimeError("corrupt transaction journal file")
        requires_owned_target = backup.exists() or not bool(item["existed"])
        if requires_owned_target and target.exists():
            same_binding = os.path.samefile(target, new_path)
            target_fp = _fingerprint(target)
            expected_fp = item.get("new_fingerprint")
            same_bytes = (isinstance(expected_fp, dict)
                          and target_fp["sha256"] == expected_fp.get("sha256")
                          and target_fp["size"] == expected_fp.get("size"))
            if not (same_binding or same_bytes):
                raise ConflictError(f"recovery target conflict: {item['relative']}")
        validated.append((item, target, new_path, backup))
    created = manifest.get("created_live_dirs", [])
    if not isinstance(created, list) or any(not isinstance(value, dict) for value in created):
        raise RuntimeError("corrupt transaction created dirs")
    for created_item in created:
        relative = str(created_item.get("relative", ""))
        pure_dir = PurePosixPath(relative)
        if (str(pure_dir) != relative or pure_dir.is_absolute() or ".." in pure_dir.parts
                or pure_dir.parts[0] not in {"20_wiki", "30_answers", "40_index", "90_logs"}):
            raise RuntimeError("corrupt transaction created dirs")
    if manifest["state"] == "committed":
        cleanup_error: OSError | None = None
        for _ in range(2):
            try:
                shutil.rmtree(journal)
                cleanup_error = None
                break
            except (OSError, RuntimeError) as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            raise cleanup_error
        return
    if manifest["state"] == "prepared":
        for item, target, new_path, backup in reversed(validated):
            if backup.exists():
                target.unlink(missing_ok=True)
                os.replace(backup, target)
                _fsync_file(target); _fsync_dir(target.parent)
            elif not bool(item["existed"]) and target.exists():
                target.unlink(); _fsync_dir(target.parent)
        for created_item in reversed(created):
            relative = str(created_item["relative"])
            directory = vault / Path(*PurePosixPath(relative).parts)
            try:
                info = directory.stat()
                identity_ok = (created_item.get("dev") is None
                               or (info.st_dev == created_item.get("dev") and info.st_ino == created_item.get("ino")))
                if identity_ok and directory.is_dir() and not _is_reparse(directory):
                    directory.rmdir()
            except OSError:
                pass
    shutil.rmtree(journal)


def _recover_locked(vault: Path, *, exclude: Path | None = None) -> None:
    _recover_cleanup_tombstones(vault)
    staging = vault / ".kb" / "staging"
    if not staging.exists():
        return
    for journal in sorted(staging.iterdir()):
        if exclude is not None and journal == exclude:
            continue
        if _is_reparse(journal) or not journal.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", journal.name):
            raise RuntimeError("unsafe transaction journal")
        if not (journal / "manifest.json").exists():
            continue
        _recover_journal(vault, journal)


def _cleanup_directory(vault: Path) -> Path:
    runtime = vault / ".kb"
    if _is_reparse(runtime) or not runtime.is_dir():
        raise RuntimeError("unsafe .kb runtime directory")
    aliases = [entry for entry in runtime.iterdir() if entry.name.casefold() == "cleanup"]
    if any(entry.name != "cleanup" for entry in aliases):
        raise RuntimeError("cleanup directory case alias")
    cleanup = runtime / "cleanup"
    if cleanup.exists():
        if _is_reparse(cleanup) or not cleanup.is_dir():
            raise RuntimeError("unsafe cleanup directory")
    else:
        cleanup.mkdir()
        _fsync_dir(runtime)
    return cleanup


def _recover_cleanup_tombstones(vault: Path) -> None:
    runtime = vault / ".kb"
    if not runtime.exists():
        return
    cleanup = _cleanup_directory(vault)
    for tombstone in sorted(cleanup.iterdir()):
        if (not re.fullmatch(r"[0-9a-f]{32}\.committed", tombstone.name)
                or _is_reparse(tombstone) or not tombstone.is_dir()
                or tombstone.parent != cleanup):
            raise RuntimeError("unsafe cleanup tombstone")
        for _ in range(2):
            try:
                shutil.rmtree(tombstone)
                _fsync_dir(cleanup)
                break
            except OSError:
                continue


def recover_pending_transactions(vault: str | Path, *, lock_timeout: float = 30.0) -> None:
    """Recover every durable prepared journal and remove committed journals."""
    transaction = ChangeTransaction(vault, lock_timeout=lock_timeout)
    with transaction._writer_lock():
        _recover_locked(transaction.vault)


class ChangeTransaction:
    """A private staging transaction for the vault's generated files only."""

    def __init__(self, vault: str | Path, *, lock_timeout: float = 30.0) -> None:
        self.vault = Path(vault).resolve(strict=False)
        if not self.vault.exists() or not self.vault.is_dir() or _is_reparse(self.vault):
            raise ValueError("vault must be an existing non-reparse directory")
        self.transaction_id = uuid.uuid4().hex
        self.stage_root = self.vault / ".kb" / "staging" / self.transaction_id
        self._staged: dict[str, Path] = {}
        self._created_live_dirs: list[Path] = []
        self.cleanup_warning: str | None = None
        self.lock_timeout = lock_timeout
        self.cleanup_tombstone = self.vault / ".kb" / "cleanup" / f"{self.transaction_id}.committed"

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
        if not thread_lock.acquire(timeout=self.lock_timeout):
            raise TimeoutError("writer lock timed out")
        try:
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
                deadline = time.monotonic() + self.lock_timeout
                while True:
                    try:
                        if os.name == "nt":
                            import msvcrt
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                            unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                        break
                    except (OSError, BlockingIOError):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("writer lock timed out")
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    unlock()
        finally:
            thread_lock.release()

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
        """Durably journal, publish, mark committed, then remove the journal."""
        if not callable(validator):
            raise ValueError("validator must be callable")
        entries = self._staged_paths()
        with self._writer_lock():
            _recover_locked(self.vault, exclude=self.stage_root)
            validator(tuple(staged for _, staged in entries))
            manifest_entries: list[dict[str, object]] = []
            preparation_metadata: list[tuple[Path, dict[str, object]]] = []
            created_live_dirs: list[dict[str, object]] = []
            try:
                new_root = self.stage_root / "new"
                backup_root = self.stage_root / "backups"
                new_root.mkdir(parents=True, exist_ok=True)
                backup_root.mkdir(exist_ok=True)
                for index, (relative, staged) in enumerate(entries):
                    target = self.vault / Path(*PurePosixPath(relative).parts)
                    self._case_safe(target)
                    self._safe_parents(target, create=False)
                    existed = target.exists()
                    fingerprint = _fingerprint(target) if existed else None
                    if fingerprint is not None:
                        preparation_metadata.append((target, fingerprint))
                    new_path = new_root / f"{index}.new"
                    shutil.copyfile(staged, new_path)
                    _fsync_file(new_path)
                    cursor = self.vault
                    for part in target.relative_to(self.vault).parts[:-1]:
                        cursor = cursor / part
                        relative_dir = cursor.relative_to(self.vault).as_posix()
                        if not cursor.exists() and all(item["relative"] != relative_dir for item in created_live_dirs):
                            created_live_dirs.append({"relative": relative_dir, "dev": None, "ino": None})
                    manifest_entries.append({"relative": relative, "existed": existed,
                        "base": fingerprint, "new": f"new/{index}.new", "backup": f"backups/{index}.bak",
                        "new_fingerprint": _fingerprint(new_path), "published": False})
                manifest = {"version": 1, "state": "prepared", "entries": manifest_entries,
                            "created_live_dirs": created_live_dirs}
                _write_manifest(self.stage_root, manifest)
                for index, item in enumerate(manifest_entries):
                    target = self.vault / Path(*PurePosixPath(str(item["relative"])).parts)
                    backup = self.stage_root / str(item["backup"])
                    new_path = self.stage_root / str(item["new"])
                    self._safe_parents(target, create=True)
                    for created_item in created_live_dirs:
                        directory = self.vault / Path(*PurePosixPath(str(created_item["relative"])).parts)
                        if directory.exists() and created_item["ino"] is None:
                            info = directory.stat()
                            created_item["dev"], created_item["ino"] = info.st_dev, info.st_ino
                    _write_manifest(self.stage_root, manifest)
                    if item["existed"]:
                        if not target.exists():
                            raise ConflictError(f"live target disappeared: {item['relative']}")
                        os.replace(target, backup)
                        _fsync_dir(target.parent); _fsync_dir(backup.parent)
                        if _fingerprint(backup) != item["base"]:
                            os.replace(backup, target)
                            raise ConflictError(f"live target changed: {item['relative']}")
                    elif target.exists():
                        raise ConflictError(f"new target appeared: {item['relative']}")
                    try:
                        os.link(new_path, target)
                    except FileExistsError as exc:
                        raise ConflictError(f"target concurrently appeared: {item['relative']}") from exc
                    _fsync_file(target); _fsync_dir(target.parent)
                    self._crash_hook("linked", index)
                    item["published"] = True
                    _write_manifest(self.stage_root, manifest)
                    self._crash_hook("published", index)
                manifest["state"] = "committed"
                _write_manifest(self.stage_root, manifest)
                self._crash_hook("committed", len(manifest_entries))
            except BaseException as original:
                try:
                    if (self.stage_root / "manifest.json").exists():
                        _recover_journal(self.vault, self.stage_root)
                    else:
                        shutil.rmtree(self.stage_root / "new", ignore_errors=True)
                        shutil.rmtree(self.stage_root / "backups", ignore_errors=True)
                        for target, base in preparation_metadata:
                            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == base["sha256"]:
                                os.chmod(target, int(base["mode"]))
                                os.utime(target, ns=(target.stat().st_atime_ns, int(base["mtime_ns"])))
                except BaseException as recovery_error:
                    if isinstance(recovery_error, ConflictError):
                        raise recovery_error from original
                    raise RollbackError(original, [recovery_error]) from original
                raise
            cleanup_error: OSError | None = None
            try:
                cleanup_root = _cleanup_directory(self.vault)
                if self.cleanup_tombstone.exists():
                    raise RuntimeError("cleanup tombstone already exists")
                os.rename(self.stage_root, self.cleanup_tombstone)
                _fsync_dir(self.stage_root.parent)
                _fsync_dir(cleanup_root)
            except OSError as exc:
                self.cleanup_warning = f"committed; tombstone move pending: {exc}"
                self._staged.clear()
                return
            for _ in range(2):
                try:
                    shutil.rmtree(self.cleanup_tombstone)
                    _fsync_dir(self.cleanup_tombstone.parent)
                    cleanup_error = None
                    break
                except OSError as exc:
                    cleanup_error = exc
            if cleanup_error is not None:
                self.cleanup_warning = f"committed; journal cleanup pending: {cleanup_error}"
            self._staged.clear()

    def _crash_hook(self, point: str, index: int) -> None:
        """Test seam used by subprocess crash-recovery tests."""

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
        index_path: Path | None = None
        index_bytes: bytes | None = None
        index_existed = False
        try:
            if not (self.vault / ".git").exists():
                subprocess.run(["git", "init"], cwd=self.vault, text=True, capture_output=True, check=True)
            top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=self.vault,
                                 text=True, capture_output=True, check=True).stdout.strip()
            if Path(top).resolve() != self.vault:
                raise RuntimeError("vault must be an independent Git repository")
            raw_index = subprocess.run(["git", "rev-parse", "--git-path", "index"], cwd=self.vault,
                                       text=True, capture_output=True, check=True).stdout.strip()
            index_path = Path(raw_index)
            if not index_path.is_absolute():
                index_path = self.vault / index_path
            index_existed = index_path.exists()
            index_bytes = index_path.read_bytes() if index_existed else None
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
        except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
            if index_path is not None:
                if index_existed and index_bytes is not None:
                    fd, name = tempfile.mkstemp(prefix=".index-restore-", dir=index_path.parent)
                    with os.fdopen(fd, "wb") as output:
                        output.write(index_bytes); output.flush(); os.fsync(output.fileno())
                    os.replace(name, index_path)
                elif not index_existed:
                    index_path.unlink(missing_ok=True)
            detail = getattr(exc, "stderr", "") or str(exc)
            raise RuntimeError(f"git commit failed: {detail}") from exc
