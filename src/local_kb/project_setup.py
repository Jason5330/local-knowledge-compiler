"""Install and verify project-local Git data protection."""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import subprocess


LOCAL_VAULT_PREFIX = "knowledgebase/"


def find_protected_paths(paths: Iterable[str]) -> tuple[str, ...]:
    protected = []
    for path in paths:
        normalized = path.replace("\\", "/").lstrip("./")
        if normalized.casefold().startswith(LOCAL_VAULT_PREFIX):
            protected.append(path)
    return tuple(protected)


@dataclass(frozen=True)
class GitProtection:
    repository: bool
    ignored: bool
    hooks_installed: bool


def _run_git(
    project: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", "-C", str(project), *arguments),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def configure_git_protection(
    project: Path,
    vault: Path,
) -> GitProtection:
    project = Path(project).resolve()
    vault = Path(vault).resolve()
    probe = _run_git(project, "rev-parse", "--show-toplevel")
    if probe.returncode != 0:
        return GitProtection(False, False, False)
    top = Path(probe.stdout.strip()).resolve()
    if top != project:
        raise RuntimeError(
            "project-local Vault protection requires the Git root"
        )
    relative = vault.relative_to(project).as_posix()
    ignore_probe = _run_git(
        project,
        "check-ignore",
        "-q",
        f"{relative}/.kb/init.lock",
    )
    if ignore_probe.returncode != 0:
        raise RuntimeError(
            "KnowledgeBase is not ignored by Git; publishing is blocked"
        )
    hooks = project / ".githooks"
    if not (
        (hooks / "pre-commit").is_file()
        and (hooks / "pre-push").is_file()
    ):
        raise RuntimeError("repository Git data guards are missing")
    configured = _run_git(
        project,
        "config",
        "--local",
        "core.hooksPath",
        ".githooks",
    )
    if configured.returncode != 0:
        raise RuntimeError(
            "could not install repository Git data guards"
        )
    return GitProtection(True, True, True)
