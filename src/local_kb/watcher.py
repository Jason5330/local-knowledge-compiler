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

    def __init__(self, required_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        if isinstance(required_seconds, bool) or required_seconds < 0:
            raise ValueError("required_seconds must be greater than or equal to 0")
        self.required_seconds = float(required_seconds)
        self.clock = clock
        self._seen: dict[Path, tuple[tuple[int, int], float, int]] = {}

    def observe(self, path: Path | str) -> bool:
        candidate = Path(path)
        try:
            if candidate.is_symlink() or _is_junction(candidate):
                raise OSError("unsafe link")
            info = os.lstat(candidate)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("not regular")
        except OSError:
            self._seen.pop(candidate, None)
            return False
        signature = (info.st_size, info.st_mtime_ns)
        now = self.clock()
        previous = self._seen.get(candidate)
        if previous is None or previous[0] != signature:
            self._seen[candidate] = (signature, now, 1)
            return False
        self._seen[candidate] = (signature, previous[1], previous[2] + 1)
        return previous[2] >= 1 and now - previous[1] >= self.required_seconds
