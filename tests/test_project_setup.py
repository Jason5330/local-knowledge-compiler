import subprocess
import sys
from pathlib import Path
import shutil

from local_kb.cli import build_vault
from local_kb.project_setup import (
    LOCAL_VAULT_PREFIX,
    configure_git_protection,
    find_protected_paths,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _git(repository: Path, *arguments: str):
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
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


def _make_repository(path: Path) -> None:
    path.mkdir()
    assert _git(path, "init").returncode == 0
    assert _git(path, "config", "user.email", "test@example.com").returncode == 0
    assert _git(path, "config", "user.name", "Test User").returncode == 0


def _install_protection_files(repository: Path) -> None:
    shutil.copy(PROJECT_ROOT / ".gitignore", repository / ".gitignore")
    shutil.copytree(PROJECT_ROOT / ".githooks", repository / ".githooks")
    (repository / "scripts").mkdir()
    shutil.copy(
        PROJECT_ROOT / "scripts" / "check-local-data.py",
        repository / "scripts" / "check-local-data.py",
    )


def test_gitignore_protects_root_vault_only(tmp_path):
    repository = tmp_path / "repo"
    _make_repository(repository)
    _install_protection_files(repository)

    root_vault = _git(
        repository,
        "check-ignore",
        "KnowledgeBase/00_inbox/private.xlsx",
    )
    fixture = _git(
        repository,
        "check-ignore",
        "tests/fixtures/KnowledgeBase/example.md",
    )

    assert root_vault.returncode == 0
    assert fixture.returncode == 1


def test_guard_rejects_force_staged_vault_data(tmp_path):
    repository = tmp_path / "repo"
    _make_repository(repository)
    _install_protection_files(repository)
    private = repository / "KnowledgeBase" / "00_inbox" / "private.xlsx"
    private.parent.mkdir(parents=True)
    private.write_text("private", encoding="utf-8")
    assert _git(repository, "add", "-f", str(private)).returncode == 0

    guarded = subprocess.run(
        (
            sys.executable,
            str(repository / "scripts" / "check-local-data.py"),
            "staged",
        ),
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert guarded.returncode == 1
    assert "KnowledgeBase/00_inbox/private.xlsx" in guarded.stderr


def test_guard_fails_closed_when_git_inspection_fails(tmp_path):
    script = PROJECT_ROOT / "scripts" / "check-local-data.py"

    guarded = subprocess.run(
        (sys.executable, str(script), "staged"),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert guarded.returncode == 1
    assert "publication blocked" in guarded.stderr


def test_configure_git_protection_installs_repository_hooks(tmp_path):
    repository = tmp_path / "repo"
    _make_repository(repository)
    _install_protection_files(repository)
    vault = repository / "KnowledgeBase"

    protection = configure_git_protection(repository, vault)
    configured = _git(repository, "config", "--local", "core.hooksPath")

    assert protection.repository is True
    assert protection.ignored is True
    assert protection.hooks_installed is True
    assert configured.stdout.strip() == ".githooks"


def test_non_git_zip_checkout_does_not_require_hooks(tmp_path):
    project = tmp_path / "downloaded-folder"
    project.mkdir()

    protection = configure_git_protection(
        project,
        project / "KnowledgeBase",
    )

    assert protection.repository is False
    assert protection.hooks_installed is False


def test_initialized_vault_content_stays_out_of_git_status(tmp_path):
    repository = tmp_path / "repo"
    _make_repository(repository)
    (repository / ".gitignore").write_text(
        "/KnowledgeBase/\n",
        encoding="utf-8",
    )
    vault = build_vault(repository / "KnowledgeBase")
    private = vault.inbox / "private.xlsx"
    private.write_bytes(b"private workbook placeholder")

    status = _git(
        repository,
        "status",
        "--short",
        "--untracked-files=all",
    )

    assert status.returncode == 0
    assert "KnowledgeBase" not in status.stdout
    assert "private.xlsx" not in status.stdout
