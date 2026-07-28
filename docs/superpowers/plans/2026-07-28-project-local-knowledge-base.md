# Project-Local Knowledge Base Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a beginner open Codex or Claude Code in the cloned repository, initialize `KnowledgeBase/` in that project with one natural-language request, keep all user data out of public Git, and receive a warning rather than a block when the project is inside OneDrive.

**Architecture:** Add a small project-context module that owns default initialization and Vault discovery, plus a separate OneDrive detector that only emits stderr warnings. Layer Git protection through the root ignore rule, a repository-local guard script, hooks installed during project-local initialization, and agent instructions; preserve all explicit-path CLI behavior.

**Tech Stack:** Python 3.13, argparse, pathlib, subprocess, pytest, Git hooks, Markdown.

---

## File Structure

### New files

- `src/local_kb/project.py` — identify initialized Vaults, choose the project-local default, and resolve implicit Vault arguments.
- `src/local_kb/onedrive.py` — detect personal/business OneDrive roots and emit the A2 warning.
- `src/local_kb/project_setup.py` — verify Git ignore protection and install repository-local hooks.
- `scripts/check-local-data.py` — reject staged or tracked files under root `KnowledgeBase/`.
- `.githooks/pre-commit` — run the staged-file guard.
- `.githooks/pre-push` — run the tracked-file guard.
- `AGENTS.md` — public Codex entrypoint and natural-language command map.
- `CLAUDE.md` — public Claude Code entrypoint and the same command map.
- `tests/test_project.py` — default path and Vault discovery tests.
- `tests/test_onedrive.py` — OneDrive detection and warning-contract tests.
- `tests/test_project_setup.py` — Git ignore, hooks, and guard tests.

### Modified files

- `src/local_kb/cli.py` — optional `kb init` path, implicit Vault resolution, OneDrive preflight, and project Git setup.
- `.gitignore` — exclude only root `/KnowledgeBase/`.
- `README.md` — make project-local onboarding the primary path.
- `docs/BEGINNER_GUIDE.zh-TW.md` — add AI Clone, ZIP rename, initialization, inbox, and OneDrive instructions.
- `docs/CLI_REFERENCE.zh-TW.md` — document agent-only default path and discovery rules.
- `AI_HANDOFF.md` — record the new operating model and safety boundary.
- `tests/test_init.py` — cover no-path initialization without regressing explicit paths.
- `tests/test_cli_operations.py` — cover implicit Vault resolution in real CLI commands.
- `tests/test_e2e.py` — enforce public docs, agent entrypoints, local data exclusion, and the beginner workflow.

---

### Task 1: Project-Local Initialization and Vault Discovery

**Files:**
- Create: `src/local_kb/project.py`
- Create: `tests/test_project.py`
- Modify: `src/local_kb/cli.py`
- Modify: `tests/test_init.py`
- Modify: `tests/test_cli_operations.py`

- [ ] **Step 1: Write failing tests for default initialization**

Add to `tests/test_init.py`:

```python
def test_main_init_without_path_creates_project_local_vault(
    tmp_path, monkeypatch, capsys
):
    project = tmp_path / "local-knowledge-compiler"
    project.mkdir()
    monkeypatch.chdir(project)

    result = main(["init"])

    assert result == 0
    vault = project / "KnowledgeBase"
    assert (vault / "80_system" / "config.toml").is_file()
    assert capsys.readouterr().out == (
        f"Initialized knowledge vault: {vault.resolve()}\n"
    )


def test_main_init_explicit_path_keeps_existing_behavior(
    tmp_path, monkeypatch, capsys
):
    project = tmp_path / "project"
    explicit = tmp_path / "custom-vault"
    project.mkdir()
    monkeypatch.chdir(project)

    result = main(["init", str(explicit)])

    assert result == 0
    assert (explicit / "80_system" / "config.toml").is_file()
    assert not (project / "KnowledgeBase").exists()
    assert capsys.readouterr().out == (
        f"Initialized knowledge vault: {explicit.resolve()}\n"
    )
```

- [ ] **Step 2: Run the initialization tests and verify failure**

Run:

```text
py -3.13 -m pytest tests/test_init.py::test_main_init_without_path_creates_project_local_vault tests/test_init.py::test_main_init_explicit_path_keeps_existing_behavior -q
```

Expected: the first test fails because argparse still requires `path`; the explicit-path test passes.

- [ ] **Step 3: Write failing tests for deterministic Vault discovery**

Create `tests/test_project.py`:

```python
from pathlib import Path

import pytest

from local_kb.cli import build_vault
from local_kb.project import default_vault_path, resolve_vault_path


def test_default_vault_path_is_knowledgebase_child(tmp_path):
    assert default_vault_path(tmp_path) == (
        tmp_path / "KnowledgeBase"
    ).resolve()


def test_explicit_vault_wins_over_project_child(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    child = build_vault(project / "KnowledgeBase").root
    explicit = build_vault(tmp_path / "explicit").root

    assert resolve_vault_path(explicit, cwd=project) == explicit
    assert child != explicit


def test_current_initialized_vault_wins_over_child_name(tmp_path):
    vault = build_vault(tmp_path / "vault").root

    assert resolve_vault_path(None, cwd=vault) == vault


def test_project_child_is_discovered(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    vault = build_vault(project / "KnowledgeBase").root

    assert resolve_vault_path(None, cwd=project) == vault


def test_missing_implicit_vault_has_actionable_error(tmp_path):
    with pytest.raises(
        ValueError,
        match="初始化本專案知識庫",
    ):
        resolve_vault_path(None, cwd=tmp_path)


def test_uninitialized_lookalike_is_rejected(tmp_path):
    lookalike = tmp_path / "KnowledgeBase"
    lookalike.mkdir()

    with pytest.raises(ValueError, match="initialized knowledge vault"):
        resolve_vault_path(lookalike, cwd=tmp_path)
```

- [ ] **Step 4: Run the discovery tests and verify import failure**

Run:

```text
py -3.13 -m pytest tests/test_project.py -q
```

Expected: collection fails because `local_kb.project` does not exist.

- [ ] **Step 5: Implement the project-context module**

Create `src/local_kb/project.py`:

```python
"""Resolve project-local knowledge Vault locations."""

from pathlib import Path


DEFAULT_VAULT_NAME = "KnowledgeBase"


def default_vault_path(cwd: Path | None = None) -> Path:
    base = Path.cwd() if cwd is None else Path(cwd)
    return (base / DEFAULT_VAULT_NAME).resolve()


def is_initialized_vault(path: Path) -> bool:
    candidate = Path(path)
    return (
        candidate.is_dir()
        and (candidate / "80_system" / "config.toml").is_file()
        and (
            candidate / "80_system" / "KNOWLEDGE_PROTOCOL.md"
        ).is_file()
    )


def resolve_vault_path(
    explicit: Path | None,
    *,
    cwd: Path | None = None,
) -> Path:
    base = (Path.cwd() if cwd is None else Path(cwd)).resolve()
    if explicit is not None:
        candidate = Path(explicit).resolve()
        if not is_initialized_vault(candidate):
            raise ValueError(
                f"not an initialized knowledge vault: {candidate}"
            )
        return candidate
    if is_initialized_vault(base):
        return base
    child = base / DEFAULT_VAULT_NAME
    if is_initialized_vault(child):
        return child.resolve()
    raise ValueError(
        "找不到已初始化的知識庫；請先對 AI 說「初始化本專案知識庫」。"
    )
```

- [ ] **Step 6: Wire optional init and implicit Vault arguments into the CLI**

In `src/local_kb/cli.py`, import:

```python
from .project import default_vault_path, resolve_vault_path
```

Change the init argument and defaulted `--vault` arguments:

```python
init_parser.add_argument("path", type=Path, nargs="?")

prepare_parser.add_argument("--vault", type=Path)
finalize_parser.add_argument("--vault", type=Path)
status_parser.add_argument("--vault", type=Path)
resume_parser.add_argument("--vault", type=Path)
lint_parser.add_argument("--vault", type=Path)
rebuild_parser.add_argument("--vault", type=Path)
```

Change initialization:

```python
if arguments.command == "init":
    target = (
        default_vault_path()
        if arguments.path is None
        else arguments.path
    )
    paths = build_vault(target)
    print(f"Initialized knowledge vault: {paths.root}")
    return 0
```

Replace the existing default-path construction for non-positional commands:

```python
def _paths_for_arguments(arguments: argparse.Namespace) -> VaultPaths:
    explicit_vault = getattr(arguments, "vault", None)
    if arguments.command == "ingest-once":
        vault_root = Path(arguments.vault).resolve()
    elif arguments.command == "watch":
        vault_root = Path(arguments.vault).resolve()
    else:
        vault_root = resolve_vault_path(explicit_vault)
    return VaultPaths(vault_root)
```

Move the existing `try` boundary above path resolution and make its first line:

```python
try:
    paths = _paths_for_arguments(arguments)
```

Keep all existing command-dispatch branches and the existing
`except Exception as error` block inside that boundary so Vault-resolution
errors use the established `kb: <message>` error contract.

- [ ] **Step 7: Add CLI-operation coverage for project-root discovery**

Add to `tests/test_cli_operations.py`:

```python
def test_status_discovers_project_local_vault(
    tmp_path, monkeypatch, capsys
):
    project = tmp_path / "project"
    project.mkdir()
    vault = build_vault(project / "KnowledgeBase")
    monkeypatch.chdir(project)

    result = main(["status"])

    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["healthy"] is True
    assert vault.root == (project / "KnowledgeBase").resolve()


def test_explicit_status_vault_overrides_project_child(
    tmp_path, monkeypatch, capsys
):
    project = tmp_path / "project"
    project.mkdir()
    build_vault(project / "KnowledgeBase")
    explicit = build_vault(tmp_path / "explicit")
    queue = DiskQueue(explicit.queue)
    queue.enqueue(explicit.inbox / "missing-source.md")
    monkeypatch.chdir(project)

    result = main(["status", "--vault", str(explicit.root)])

    assert result == 2
    report = json.loads(capsys.readouterr().out)
    assert report["actionable_count"] == 1
```

- [ ] **Step 8: Run project and CLI tests**

Run:

```text
py -3.13 -m pytest tests/test_project.py tests/test_init.py tests/test_cli_operations.py -q
```

Expected: all tests pass.

- [ ] **Step 9: Commit project-local discovery**

```text
git add src/local_kb/project.py src/local_kb/cli.py tests/test_project.py tests/test_init.py tests/test_cli_operations.py
git commit -m "feat: add project-local vault discovery"
```

---

### Task 2: OneDrive A2 Warning

**Files:**
- Create: `src/local_kb/onedrive.py`
- Create: `tests/test_onedrive.py`
- Modify: `src/local_kb/cli.py`

- [ ] **Step 1: Write failing OneDrive detector tests**

Create `tests/test_onedrive.py`:

```python
from pathlib import Path

from local_kb.onedrive import (
    ONEDRIVE_WARNING,
    find_onedrive_root,
    warn_if_onedrive,
)


def test_personal_onedrive_child_is_detected(tmp_path):
    root = tmp_path / "OneDrive"
    vault = root / "project" / "KnowledgeBase"
    environment = {"OneDrive": str(root)}

    assert find_onedrive_root(vault, environment) == root.resolve()


def test_business_onedrive_child_is_detected(tmp_path):
    root = tmp_path / "OneDrive - Example Company"
    vault = root / "project" / "KnowledgeBase"
    environment = {"OneDriveCommercial": str(root)}

    assert find_onedrive_root(vault, environment) == root.resolve()


def test_similar_prefix_outside_root_is_not_detected(tmp_path):
    root = tmp_path / "OneDrive"
    vault = tmp_path / "OneDrive-Backup" / "KnowledgeBase"

    assert find_onedrive_root(
        vault,
        {"OneDrive": str(root)},
    ) is None


def test_empty_or_invalid_environment_values_are_ignored(tmp_path):
    assert find_onedrive_root(
        tmp_path / "KnowledgeBase",
        {
            "OneDrive": "",
            "OneDriveConsumer": "\x00",
        },
    ) is None


def test_warning_is_written_only_to_supplied_stream(tmp_path):
    from io import StringIO

    root = tmp_path / "OneDrive"
    stream = StringIO()

    matched = warn_if_onedrive(
        root / "KnowledgeBase",
        environ={"OneDrive": str(root)},
        stream=stream,
    )

    assert matched is True
    assert stream.getvalue() == ONEDRIVE_WARNING + "\n"


def test_non_onedrive_path_emits_nothing(tmp_path):
    from io import StringIO

    stream = StringIO()

    matched = warn_if_onedrive(
        tmp_path / "KnowledgeBase",
        environ={},
        stream=stream,
    )

    assert matched is False
    assert stream.getvalue() == ""
```

- [ ] **Step 2: Run detector tests and verify import failure**

Run:

```text
py -3.13 -m pytest tests/test_onedrive.py -q
```

Expected: collection fails because `local_kb.onedrive` does not exist.

- [ ] **Step 3: Implement the detector and warning**

Create `src/local_kb/onedrive.py`:

```python
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
```

- [ ] **Step 4: Add failing CLI tests for stderr-only warnings**

Append to `tests/test_onedrive.py`:

```python
import json

from local_kb.cli import build_vault, main


def test_status_warning_does_not_corrupt_json_or_exit_code(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "OneDrive"
    project = root / "project"
    project.mkdir(parents=True)
    build_vault(project / "KnowledgeBase")
    monkeypatch.setenv("OneDrive", str(root))
    monkeypatch.chdir(project)

    result = main(["status"])

    captured = capsys.readouterr()
    assert result == 0
    assert json.loads(captured.out)["healthy"] is True
    assert captured.err == ONEDRIVE_WARNING + "\n"


def test_init_warning_continues_and_creates_vault(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "OneDrive"
    project = root / "project"
    project.mkdir(parents=True)
    monkeypatch.setenv("OneDriveConsumer", str(root))
    monkeypatch.chdir(project)

    result = main(["init"])

    captured = capsys.readouterr()
    assert result == 0
    assert (project / "KnowledgeBase").is_dir()
    assert captured.err == ONEDRIVE_WARNING + "\n"
```

- [ ] **Step 5: Wire warnings into every command without changing stdout**

In `src/local_kb/cli.py`, import:

```python
from .onedrive import warn_if_onedrive
```

After project-local or explicit initialization:

```python
paths = build_vault(target)
warn_if_onedrive(paths.root)
print(f"Initialized knowledge vault: {paths.root}")
return 0
```

After constructing `paths` for every other command:

```python
paths = VaultPaths(vault_root)
warn_if_onedrive(paths.root)
```

Do not add warning fields to status or lint JSON.

- [ ] **Step 6: Run OneDrive and CLI regression tests**

Run:

```text
py -3.13 -m pytest tests/test_onedrive.py tests/test_init.py tests/test_cli_operations.py -q
```

Expected: all tests pass and status stdout remains valid JSON.

- [ ] **Step 7: Commit OneDrive warning support**

```text
git add src/local_kb/onedrive.py src/local_kb/cli.py tests/test_onedrive.py
git commit -m "feat: warn for OneDrive vault paths"
```

---

### Task 3: Local User-Data Git Guard

**Files:**
- Modify: `.gitignore`
- Create: `scripts/check-local-data.py`
- Create: `.githooks/pre-commit`
- Create: `.githooks/pre-push`
- Create: `src/local_kb/project_setup.py`
- Create: `tests/test_project_setup.py`
- Modify: `src/local_kb/cli.py`

- [ ] **Step 1: Write failing tests for path classification**

Create `tests/test_project_setup.py`:

```python
import subprocess
import sys
from pathlib import Path

import pytest

from local_kb.project_setup import (
    LOCAL_VAULT_PREFIX,
    find_protected_paths,
)


def test_only_root_knowledgebase_is_protected():
    paths = (
        "KnowledgeBase/00_inbox/private.xlsx",
        "knowledgebase/20_wiki/private.md",
        "tests/fixtures/KnowledgeBase/example.md",
        "docs/KnowledgeBase.md",
    )

    assert find_protected_paths(paths) == (
        "KnowledgeBase/00_inbox/private.xlsx",
        "knowledgebase/20_wiki/private.md",
    )
    assert LOCAL_VAULT_PREFIX == "knowledgebase/"
```

- [ ] **Step 2: Run the classification test and verify import failure**

Run:

```text
py -3.13 -m pytest tests/test_project_setup.py::test_only_root_knowledgebase_is_protected -q
```

Expected: collection fails because `local_kb.project_setup` does not exist.

- [ ] **Step 3: Implement reusable path classification**

Create `src/local_kb/project_setup.py` with:

```python
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


def _run_git(project: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
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
        raise RuntimeError("could not install repository Git data guards")
    return GitProtection(True, True, True)
```

- [ ] **Step 4: Add the root ignore rule**

Append exactly this line to `.gitignore`:

```text
/KnowledgeBase/
```

Do not use `KnowledgeBase/` without the leading slash.

- [ ] **Step 5: Write the guard script**

Create `scripts/check-local-data.py`:

```python
"""Block accidental Git publication of project-local user data."""

import argparse
from pathlib import Path
import subprocess
import sys


PREFIX = "knowledgebase/"


def protected(paths: list[str]) -> list[str]:
    return [
        path
        for path in paths
        if path.replace("\\", "/")
        .lstrip("./")
        .casefold()
        .startswith(PREFIX)
    ]


def git_paths(mode: str) -> list[str]:
    if mode == "staged":
        command = (
            "git",
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMR",
            "-z",
        )
    else:
        command = ("git", "ls-files", "-z")
    completed = subprocess.run(
        command,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        sys.stderr.write("Could not inspect Git paths; publication blocked.\n")
        return ["<git-inspection-failed>"]
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in completed.stdout.split(b"\0")
        if item
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=("staged", "tracked"),
    )
    arguments = parser.parse_args(argv)
    violations = protected(git_paths(arguments.mode))
    if not violations:
        return 0
    sys.stderr.write(
        "Blocked: local KnowledgeBase data must never be published.\n"
    )
    for path in violations:
        sys.stderr.write(f"- {path}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Add repository-local hooks**

Create `.githooks/pre-commit`:

```sh
#!/usr/bin/env sh
exec python scripts/check-local-data.py staged
```

Create `.githooks/pre-push`:

```sh
#!/usr/bin/env sh
exec python scripts/check-local-data.py tracked
```

Mark both executable in Git:

```text
git update-index --add --chmod=+x .githooks/pre-commit .githooks/pre-push
```

- [ ] **Step 7: Add real temporary-repository tests**

Append to `tests/test_project_setup.py`:

```python
def _git(repository: Path, *arguments: str):
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_gitignore_rule_ignores_only_root_vault(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    (repository / ".gitignore").write_text(
        "/KnowledgeBase/\n",
        encoding="utf-8",
    )
    root_data = repository / "KnowledgeBase" / "00_inbox" / "private.xlsx"
    fixture = (
        repository
        / "tests"
        / "fixtures"
        / "KnowledgeBase"
        / "example.md"
    )
    root_data.parent.mkdir(parents=True)
    fixture.parent.mkdir(parents=True)
    root_data.write_bytes(b"private")
    fixture.write_text("public fixture", encoding="utf-8")

    ignored = _git(
        repository,
        "check-ignore",
        str(root_data.relative_to(repository)),
    )
    fixture_check = _git(
        repository,
        "check-ignore",
        str(fixture.relative_to(repository)),
    )

    assert ignored.returncode == 0
    assert fixture_check.returncode == 1


def test_guard_rejects_forced_staged_vault_file(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    source_script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "check-local-data.py"
    )
    target_script = repository / "scripts" / "check-local-data.py"
    target_script.parent.mkdir()
    target_script.write_bytes(source_script.read_bytes())
    private = repository / "KnowledgeBase" / "private.txt"
    private.parent.mkdir()
    private.write_text("private", encoding="utf-8")
    assert _git(repository, "add", "-f", "KnowledgeBase/private.txt").returncode == 0

    guarded = subprocess.run(
        (
            sys.executable,
            str(target_script),
            "staged",
        ),
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert guarded.returncode == 1
    assert "must never be published" in guarded.stderr
    assert "KnowledgeBase/private.txt" in guarded.stderr
```

- [ ] **Step 8: Test Git-protection configuration**

Append to `tests/test_project_setup.py`:

```python
from local_kb.cli import build_vault
from local_kb.project_setup import configure_git_protection


def test_project_setup_verifies_ignore_and_installs_hooks(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    (repository / ".gitignore").write_text(
        "/KnowledgeBase/\n",
        encoding="utf-8",
    )
    hooks = repository / ".githooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\n", encoding="ascii")
    (hooks / "pre-push").write_text("#!/bin/sh\n", encoding="ascii")
    vault = build_vault(repository / "KnowledgeBase").root

    result = configure_git_protection(repository, vault)

    assert result.repository is True
    assert result.ignored is True
    assert result.hooks_installed is True
    configured = _git(
        repository,
        "config",
        "--local",
        "--get",
        "core.hooksPath",
    )
    assert configured.stdout.strip() == ".githooks"


def test_non_git_zip_checkout_skips_hooks(tmp_path):
    project = tmp_path / "downloaded-zip"
    project.mkdir()
    vault = build_vault(project / "KnowledgeBase").root

    result = configure_git_protection(project, vault)

    assert result.repository is False
    assert result.hooks_installed is False
```

- [ ] **Step 9: Connect Git protection only to no-path project initialization**

In `src/local_kb/cli.py`, import:

```python
from .project_setup import configure_git_protection
```

In the init branch:

```python
project_local = arguments.path is None
target = (
    default_vault_path()
    if project_local
    else arguments.path
)
paths = build_vault(target)
warn_if_onedrive(paths.root)
if project_local:
    try:
        protection = configure_git_protection(
            Path.cwd(),
            paths.root,
        )
    except RuntimeError as error:
        print(f"資料保護提醒：{error}", file=sys.stderr)
        print(f"Initialized knowledge vault: {paths.root}")
        return 2
print(f"Initialized knowledge vault: {paths.root}")
return 0
```

- [ ] **Step 10: Run Git protection tests**

Run:

```text
py -3.13 -m pytest tests/test_project_setup.py tests/test_init.py -q
```

Expected: all tests pass. The two pre-existing untracked PNG files remain unmodified and untracked.

- [ ] **Step 11: Commit Git protection**

```text
git add .gitignore .githooks/pre-commit .githooks/pre-push scripts/check-local-data.py src/local_kb/project_setup.py src/local_kb/cli.py tests/test_project_setup.py tests/test_init.py
git commit -m "feat: protect project-local user data"
```

---

### Task 4: Public Codex and Claude Code Entrypoints

**Files:**
- Create: `AGENTS.md`
- Create: `CLAUDE.md`
- Modify: `tests/test_e2e.py`

- [ ] **Step 1: Write failing entrypoint documentation tests**

Add to `tests/test_e2e.py`:

```python
def test_root_agent_entrypoints_share_beginner_commands_and_git_boundary():
    repository = Path(__file__).resolve().parents[1]
    agents = (repository / "AGENTS.md").read_text(encoding="utf-8")
    claude = (repository / "CLAUDE.md").read_text(encoding="utf-8")

    for document in (agents, claude):
        assert "初始化本專案知識庫" in document
        assert "整理知識庫裡的新資料" in document
        assert "使用知識庫回答：" in document
        assert "檢查知識庫是否正常" in document
        assert "KnowledgeBase/00_inbox" in document
        assert "不得 stage、commit 或 push" in document
        assert "`KnowledgeBase/`" in document
        assert "KnowledgeBase/80_system/KNOWLEDGE_PROTOCOL.md" in document
        assert "只查詢、不保存" in document
```

- [ ] **Step 2: Run the entrypoint test and verify missing files**

Run:

```text
py -3.13 -m pytest tests/test_e2e.py::test_root_agent_entrypoints_share_beginner_commands_and_git_boundary -q
```

Expected: FAIL because root `AGENTS.md` and `CLAUDE.md` do not exist.

- [ ] **Step 3: Create the Codex entrypoint**

Create `AGENTS.md`:

```markdown
# Local Knowledge Compiler — Codex 入口

本專案服務技術初學者。代理直接完成本機操作，不要求使用者輸入終端機指令。

## 白話口令

- 「初始化本專案知識庫」：在專案根目錄執行無路徑 `kb init`，再執行 status 與 lint。
- 「整理知識庫裡的新資料」：只處理 `KnowledgeBase/00_inbox/` 的直接檔案；外部唯一原檔必須先複製，ingest-once 只接收副本。
- 「使用知識庫回答：問題」：先 prepare，只依本地證據回答；建立引用後 finalize。
- 「只查詢、不保存」：prepare 後回答，但不得 finalize。
- 「檢查知識庫是否正常」：執行 status 與 lint，以白話分類回報。
- 「繼續處理知識庫中卡住的工作」：先讀 status 與 handoff，再 resume。

`KnowledgeBase/` 存在時，處理知識任務前必須完整讀取並遵守
`KnowledgeBase/80_system/KNOWLEDGE_PROTOCOL.md`。

## 私料邊界

`KnowledgeBase/` 與其中所有資料只屬於本機。不得 stage、commit 或 push
`KnowledgeBase/`，也不得把其內容複製到公開 README、文件、Issue、PR、commit 或
聊天紀錄。任何 Git 發布前先執行本專案的資料防護檢查。
```

- [ ] **Step 4: Create the Claude Code entrypoint**

Create `CLAUDE.md` with the same commands and boundaries, changing only the title:

```markdown
# Local Knowledge Compiler — Claude Code 入口

本專案服務技術初學者。代理直接完成本機操作，不要求使用者輸入終端機指令。

## 白話口令

- 「初始化本專案知識庫」：在專案根目錄執行無路徑 `kb init`，再執行 status 與 lint。
- 「整理知識庫裡的新資料」：只處理 `KnowledgeBase/00_inbox/` 的直接檔案；外部唯一原檔必須先複製，ingest-once 只接收副本。
- 「使用知識庫回答：問題」：先 prepare，只依本地證據回答；建立引用後 finalize。
- 「只查詢、不保存」：prepare 後回答，但不得 finalize。
- 「檢查知識庫是否正常」：執行 status 與 lint，以白話分類回報。
- 「繼續處理知識庫中卡住的工作」：先讀 status 與 handoff，再 resume。

`KnowledgeBase/` 存在時，處理知識任務前必須完整讀取並遵守
`KnowledgeBase/80_system/KNOWLEDGE_PROTOCOL.md`。

## 私料邊界

`KnowledgeBase/` 與其中所有資料只屬於本機。不得 stage、commit 或 push
`KnowledgeBase/`，也不得把其內容複製到公開 README、文件、Issue、PR、commit 或
聊天紀錄。任何 Git 發布前先執行本專案的資料防護檢查。
```

- [ ] **Step 5: Run the entrypoint test**

Run:

```text
py -3.13 -m pytest tests/test_e2e.py::test_root_agent_entrypoints_share_beginner_commands_and_git_boundary -q
```

Expected: PASS.

- [ ] **Step 6: Commit the shared agent protocol**

```text
git add AGENTS.md CLAUDE.md tests/test_e2e.py
git commit -m "docs: add shared beginner agent commands"
```

---

### Task 5: Beginner Documentation and Handoff

**Files:**
- Modify: `README.md`
- Modify: `docs/BEGINNER_GUIDE.zh-TW.md`
- Modify: `docs/CLI_REFERENCE.zh-TW.md`
- Modify: `AI_HANDOFF.md`
- Modify: `tests/test_e2e.py`
- Modify: `tests/test_cli_operations.py`

- [ ] **Step 1: Replace old fixed-path assertions with the approved workflow**

In `tests/test_e2e.py`, replace the assertion:

```python
assert "兩者都使用 `C:\\KnowledgeBase`" in readme
```

with:

```python
for required in (
    "local-knowledge-compiler",
    "KnowledgeBase",
    "初始化本專案知識庫",
    "整理知識庫裡的新資料",
    "00_inbox",
    "OneDrive",
    "只提醒",
    "Download ZIP",
    "-master",
    "Clone",
    "/KnowledgeBase/",
):
    assert required in readme + guide
assert "固定使用 `C:\\KnowledgeBase`" not in readme
assert "固定使用 `C:\\KnowledgeBase`" not in guide
```

Add:

```python
def test_beginner_docs_keep_local_vault_out_of_public_git():
    repository = Path(__file__).resolve().parents[1]
    documents = [
        (repository / "README.md").read_text(encoding="utf-8"),
        (
            repository / "docs" / "BEGINNER_GUIDE.zh-TW.md"
        ).read_text(encoding="utf-8"),
        (repository / "AI_HANDOFF.md").read_text(encoding="utf-8"),
    ]
    combined = "\n".join(documents)

    assert "KnowledgeBase/` 與用戶資料只留在本機" in combined
    assert "不得上傳到公開 GitHub" in combined
    assert "Repo 放在 C 槽" in combined
    assert "Repo 放在 D 槽" in combined
    assert "OneDrive 以外" in combined
```

- [ ] **Step 2: Run documentation tests and verify failure**

Run:

```text
py -3.13 -m pytest tests/test_e2e.py::test_beginner_docs_use_ai_driven_installation_and_safe_excel_copy tests/test_e2e.py::test_beginner_docs_keep_local_vault_out_of_public_git -q
```

Expected: FAIL because the published guides still describe `C:\KnowledgeBase` as the default.

- [ ] **Step 3: Rewrite README onboarding**

Make the first workflow in `README.md`:

```text
選擇 C 槽或 D 槽的父資料夾
→ 在 Codex／Claude Code 貼「下載並初始化」提示詞
→ AI Clone 成 local-knowledge-compiler
→ AI 執行「初始化本專案知識庫」
→ 建立 local-knowledge-compiler/KnowledgeBase
→ 使用者把新資料放入 KnowledgeBase/00_inbox
→ 對 AI 說「整理知識庫裡的新資料」
```

Add explicit boundaries:

```text
Repo 放在 C 槽 → Vault 跟隨 C 槽
Repo 放在 D 槽 → Vault 跟隨 D 槽
KnowledgeBase/ 與用戶資料只留在本機
不得上傳到公開 GitHub
OneDrive 內只提醒，不阻止
```

Document Clone as primary and Download ZIP plus AI rename from `-master` as fallback.

- [ ] **Step 4: Rewrite the beginner prompts**

In `docs/BEGINNER_GUIDE.zh-TW.md`, provide exact copy-paste prompts for:

```text
請把公開 Repo https://github.com/Jason5330/local-knowledge-compiler
下載到目前資料夾，資料夾固定命名 local-knowledge-compiler。
不要使用帶有 -master 的最終資料夾名稱。
下載後進入專案，安裝執行環境並初始化本專案知識庫。
KnowledgeBase/ 與其中所有資料只留在本機，不得提交或上傳 GitHub。
完成後執行 status 與 lint，再用白話回報。
```

Add ZIP fallback:

```text
如果使用者已用 Download ZIP，先確認 local-knowledge-compiler 不存在，
再把 local-knowledge-compiler-master 安全改名；不得覆蓋同名資料夾。
```

Add the four natural-language daily commands verbatim.

- [ ] **Step 5: Update the agent-only CLI reference**

In `docs/CLI_REFERENCE.zh-TW.md`, document:

```text
kb init
→ <cwd>/KnowledgeBase

kb init <path>
→ explicit path, unchanged

implicit lookup
→ explicit --vault
→ initialized cwd
→ initialized cwd/KnowledgeBase
→ actionable failure
```

Document stderr-only OneDrive warnings and project Git protection setup.

- [ ] **Step 6: Update AI handoff**

In `AI_HANDOFF.md`, replace fixed default Vault claims with:

```text
The authoritative Vault is the initialized KnowledgeBase/ under the active
project clone unless the user explicitly supplied another Vault.
```

Record:

- public Repo versus local private Vault;
- Clone naming versus ZIP `-master`;
- OneDrive mode A2;
- implicit Vault discovery order;
- Git hooks and guard command;
- natural-language command map.

- [ ] **Step 7: Remove stale README status-command test coupling**

In `tests/test_cli_operations.py`, change
`test_beginner_readme_documents_status_resume_and_exit_codes` to read
`docs/CLI_REFERENCE.zh-TW.md` for exact internal executable syntax, while README
only needs the natural-language health command:

```python
def test_agent_cli_reference_documents_status_resume_and_exit_codes():
    repository = Path(__file__).resolve().parents[1]
    reference = (
        repository / "docs" / "CLI_REFERENCE.zh-TW.md"
    ).read_text(encoding="utf-8")
    readme = (repository / "README.md").read_text(encoding="utf-8")

    assert "kb.exe status --vault" in reference
    assert "kb.exe resume --vault" in reference
    assert "--job-id" in reference
    assert "Exit code `0`" in reference
    assert "Exit code `1`" in reference
    assert "Exit code `2`" in reference
    assert "pending_attention" in reference
    assert "檢查知識庫是否正常" in readme
```

- [ ] **Step 8: Run all documentation tests**

Run:

```text
py -3.13 -m pytest tests/test_e2e.py tests/test_cli_operations.py -q
```

Expected: all tests pass, with only environment-specific skips.

- [ ] **Step 9: Commit documentation**

```text
git add README.md docs/BEGINNER_GUIDE.zh-TW.md docs/CLI_REFERENCE.zh-TW.md AI_HANDOFF.md tests/test_e2e.py tests/test_cli_operations.py
git commit -m "docs: teach project-local beginner workflow"
```

---

### Task 6: End-to-End Project-Local Workflow

**Files:**
- Modify: `tests/test_e2e.py`
- Modify: `tests/test_project_setup.py`

- [ ] **Step 1: Add a full project-local initialization and query test**

Add to `tests/test_e2e.py`:

```python
def test_project_local_vault_can_ingest_and_prepare_from_project_root(
    tmp_path, monkeypatch, capsys
):
    project = tmp_path / "local-knowledge-compiler"
    project.mkdir()
    monkeypatch.chdir(project)

    assert main(["init"]) == 0
    capsys.readouterr()
    vault = VaultPaths(project / "KnowledgeBase")
    vault.config.write_text(
        vault.config.read_text(encoding="utf-8").replace(
            'provider = "claude"',
            'provider = "manual"',
        ),
        encoding="utf-8",
    )
    source = vault.inbox / "decision.md"
    source.write_text(
        "The approved local choice is B.",
        encoding="utf-8",
    )

    ingested = main(
        [
            "ingest-once",
            str(vault.root),
            str(source),
            "--space",
            "work",
        ]
    )
    assert ingested == 2
    capsys.readouterr()

    packet = vault.runtime / "project-packet.json"
    prepared = main(
        [
            "prepare",
            "What local choice was approved?",
            "--space",
            "work",
            "--output",
            str(packet),
        ]
    )

    assert prepared == 0
    document = json.loads(packet.read_text(encoding="utf-8"))
    assert any(
        "approved local choice is B" in item.get("text", "")
        for item in document["evidence"]
    )
```

- [ ] **Step 2: Add a Git status assertion for real Vault content**

Append to `tests/test_project_setup.py`:

```python
def test_initialized_vault_content_stays_out_of_git_status(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    (repository / ".gitignore").write_text(
        "/KnowledgeBase/\n",
        encoding="utf-8",
    )
    vault = build_vault(repository / "KnowledgeBase")
    private = vault.inbox / "private.xlsx"
    private.write_bytes(b"private workbook placeholder")

    status = _git(repository, "status", "--short", "--untracked-files=all")

    assert status.returncode == 0
    assert "KnowledgeBase" not in status.stdout
    assert "private.xlsx" not in status.stdout
```

- [ ] **Step 3: Run the new end-to-end tests**

Run:

```text
py -3.13 -m pytest tests/test_e2e.py::test_project_local_vault_can_ingest_and_prepare_from_project_root tests/test_project_setup.py::test_initialized_vault_content_stays_out_of_git_status -q
```

Expected: 2 passed.

- [ ] **Step 4: Run focused feature coverage**

Run:

```text
py -3.13 -m pytest tests/test_project.py tests/test_onedrive.py tests/test_project_setup.py tests/test_init.py tests/test_cli_operations.py tests/test_e2e.py -q
```

Expected: all tests pass, with Windows-only or environment-specific skips allowed.

- [ ] **Step 5: Commit end-to-end coverage**

```text
git add tests/test_e2e.py tests/test_project_setup.py
git commit -m "test: cover project-local knowledge workflow"
```

---

### Task 7: Full Verification and Public Release

**Files:**
- Verify: all tracked project files
- Preserve untracked: `AI-Wiki-小白圖解.png`
- Preserve untracked: `AI-Wiki-風險提醒-小白圖解.png`

- [ ] **Step 1: Verify the working-tree scope**

Run:

```text
git status --short
```

Expected: the two user-owned PNG files may remain untracked. No generated
`KnowledgeBase/` content may be staged or tracked.

- [ ] **Step 2: Run the complete test suite**

Run:

```text
py -3.13 -m pytest -q
```

Expected: all tests pass; report the exact passed and skipped counts.

- [ ] **Step 3: Verify formatting and forbidden tracked paths**

Run:

```text
git diff --check
py -3.13 scripts/check-local-data.py tracked
git ls-files KnowledgeBase
```

Expected:

- `git diff --check` exits 0.
- the guard exits 0.
- `git ls-files KnowledgeBase` prints nothing.

- [ ] **Step 4: Run public-repository redaction**

Stage only implementation files from Tasks 1–6, then run the repository's
public redaction scanner against the staged diff. Expected result:

```json
{
  "findings": [],
  "counts": {
    "HIGH": 0,
    "MEDIUM": 0,
    "LOW": 0,
    "WARN": 0
  },
  "repoVisibility": "public",
  "oversize": false
}
```

If findings appear, unstage, correct the exact finding, and repeat before any
push.

- [ ] **Step 5: Confirm every task has a commit**

Run:

```text
git log --oneline --max-count=8
```

Expected commits include:

```text
test: cover project-local knowledge workflow
docs: teach project-local beginner workflow
docs: add shared beginner agent commands
feat: protect project-local user data
feat: warn for OneDrive vault paths
feat: add project-local vault discovery
```

- [ ] **Step 6: Push the implementation branch and review**

Push the implementation branch, inspect the complete diff against `master`, and
run the repository's review workflow. Do not merge if any Critical or Important
issue remains.

- [ ] **Step 7: Update project memory after confirmed release**

Record:

- final commit;
- exact test counts;
- project-local default;
- OneDrive A2 warning behavior;
- Git local-data protection;
- any remaining limitations.

Do not record private Vault contents or local file names beyond the already
known untracked documentation images.
