"""Best-effort OneDrive path detection without changing OneDrive."""

from collections.abc import Mapping
import os
from pathlib import Path
import sys
from typing import TextIO


ONEDRIVE_ENVIRONMENT_KEYS = (
    "OneDrive",
    "OneDriveConsumer",
    "OneDriveCommercial",
)
ONEDRIVE_WARNING = (
    "提醒：知識庫位於 OneDrive 內，可能會同步到其他裝置。\n"
    "這次操作仍會繼續。\n"
    "如果你希望資料只留在本機，請把整個專案放到 "
    "OneDrive 以外的資料夾。"
)


def _resolved(path: Path) -> Path | None:
    try:
        return Path(path).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        if os.name != "nt":
            return False
        path_text = os.path.normcase(str(path))
        root_text = os.path.normcase(str(root))
        try:
            return os.path.commonpath(
                (path_text, root_text)
            ) == root_text
        except ValueError:
            return False


def find_onedrive_root(
    vault: Path,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    values = os.environ if environ is None else environ
    candidate = _resolved(Path(vault))
    if candidate is None:
        return None
    for key in ONEDRIVE_ENVIRONMENT_KEYS:
        raw = values.get(key, "").strip()
        if not raw or "\x00" in raw:
            continue
        root = _resolved(Path(raw))
        if root is not None and _is_within(candidate, root):
            return root
    return None


def warn_if_onedrive(
    vault: Path,
    *,
    environ: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> bool:
    if find_onedrive_root(vault, environ) is None:
        return False
    output = sys.stderr if stream is None else stream
    print(ONEDRIVE_WARNING, file=output)
    return True
