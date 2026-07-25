"""Safe polling primitives for files that may still be written."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import time
from typing import Callable


def _is_junction(path: Path) -> bool:
    check = getattr(path, "is_junction", None)
    try:
        return bool(check()) if callable(check) else False
    except OSError:
        return True


class StableTracker:
    """Report a file only after two unchanged observations over a duration."""

    def __init__(
        self,
        required_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        trusted_root: Path | str | None = None,
    ) -> None:
        if isinstance(required_seconds, bool) or required_seconds < 0:
            raise ValueError("required_seconds must be greater than or equal to 0")
        self.required_seconds = float(required_seconds)
        self.clock = clock
        self._seen: dict[Path, tuple[tuple[int, int, int, int], float, int]] = {}
        self.trusted_root: Path | None = None
        self._root_identity: tuple[int, int] | None = None
        if trusted_root is not None:
            self.bind_trusted_root(trusted_root)

    def bind_trusted_root(self, root: Path | str) -> None:
        """Pin a safe inbox directory; later replacement invalidates observations."""
        candidate = Path(os.path.abspath(os.fspath(root)))
        try:
            info = self._safe_directory(candidate)
        except OSError as error:
            raise ValueError("trusted inbox root must be safe") from error
        self.trusted_root = candidate
        self._root_identity = (info.st_dev, info.st_ino)
        self._seen.clear()

    def iter_trusted_children(self) -> list[Path]:
        """List direct children only after revalidating the pinned inbox root."""
        root = self._checked_root()
        before = os.lstat(root)
        children = list(root.iterdir())
        after = self._safe_directory(root)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("trusted inbox root changed during scan")
        return children

    def observe(self, path: Path | str) -> bool:
        candidate = Path(os.path.abspath(os.fspath(path)))
        try:
            if self.trusted_root is not None:
                root = self._checked_root()
                if candidate.parent != root:
                    raise OSError("candidate is not a direct trusted inbox child")
                self._safe_path_components(candidate, root)
            elif candidate.is_symlink() or _is_junction(candidate):
                raise OSError("unsafe link")
            info = os.lstat(candidate)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("not regular")
        except OSError:
            self._seen.pop(candidate, None)
            return False
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        now = self.clock()
        previous = self._seen.get(candidate)
        if previous is None or previous[0] != signature:
            self._seen[candidate] = (signature, now, 1)
            return False
        self._seen[candidate] = (signature, previous[1], previous[2] + 1)
        return previous[2] >= 1 and now - previous[1] >= self.required_seconds

    def forget(self, path: Path | str) -> None:
        self._seen.pop(Path(os.path.abspath(os.fspath(path))), None)

    def prune(self) -> None:
        for path in list(self._seen):
            if not os.path.lexists(path):
                self._seen.pop(path, None)

    def _checked_root(self) -> Path:
        if self.trusted_root is None or self._root_identity is None:
            raise ValueError("trusted inbox root is not configured")
        info = self._safe_directory(self.trusted_root)
        if (info.st_dev, info.st_ino) != self._root_identity:
            raise ValueError("trusted inbox root was replaced")
        return self.trusted_root

    @staticmethod
    def _safe_directory(path: Path):
        StableTracker._safe_path_components(path, path)
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("trusted inbox root must be a safe directory")
        return info

    @staticmethod
    def _safe_path_components(path: Path, root: Path) -> None:
        """Reject links/reparse points in every lexical component through *path*."""
        absolute = Path(os.path.abspath(os.fspath(path)))
        root_absolute = Path(os.path.abspath(os.fspath(root)))
        try:
            absolute.relative_to(root_absolute)
        except ValueError:
            raise OSError("candidate escapes trusted inbox root") from None
        current = Path(absolute.anchor)
        for component in absolute.parts[1:]:
            current = current / component
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode) or _is_junction(current):
                raise OSError("trusted inbox path contains a link or reparse point")
