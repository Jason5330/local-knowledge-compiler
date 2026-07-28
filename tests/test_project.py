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
