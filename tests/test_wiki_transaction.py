from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from local_kb.transaction import ChangeTransaction, RollbackError
from local_kb.wiki import WikiPage, render_page, validate_page


def page(**changes: object) -> WikiPage:
    base = WikiPage(
        "wiki-1", "A safe title", "concept", "personal", "high", ("source-1",),
        "The current, supported conclusion.", "", "2026-07-25: page created.",
        aliases=("Safe alias",), updated_at="2026-07-25T12:30:00+00:00", related=("wiki-2",),
    )
    return replace(base, **changes)


def test_valid_page_renders_deterministic_safe_schema() -> None:
    rendered = render_page(page(title="Title: \"quoted\""))
    assert rendered == render_page(page(title="Title: \"quoted\""))
    assert rendered.startswith("---\nid: \"wiki-1\"\ntitle: \"Title: \\\"quoted\\\"\"\n")
    assert "aliases:\n  - \"Safe alias\"\n" in rendered
    assert "type: \"concept\"\nspace: \"personal\"\nstatus: \"active\"\n" in rendered
    assert "source_ids:\n  - \"source-1\"\n---\n\n## Current State\n" in rendered
    assert "## Evidence\n\n- source-1\n" in rendered
    assert "## Conflicts and Gaps\n\n無\n" in rendered
    assert "## Related\n\n- wiki-2\n" in rendered
    assert rendered.endswith("## Timeline\n\n2026-07-25: page created.\n")


def test_legacy_nine_positional_page_normalizes_lists_and_multiline_bodies() -> None:
    legacy = WikiPage("wiki-legacy", "Legacy", "topic", "work", "medium", ["source-1"],
                      "First line\r\nSecond line\rThird line", "No conflicts", "One\rTwo")
    assert isinstance(legacy.source_ids, tuple)
    assert legacy.updated_at
    rendered = render_page(legacy)
    assert "First line\nSecond line\nThird line" in rendered
    assert "## Conflicts and Gaps\n\nNo conflicts" in rendered
    assert "\r" not in rendered
    assert render_page(legacy) == render_page(WikiPage("wiki-legacy", "Legacy", "topic", "work", "medium", ["source-1"], "First line\r\nSecond line\rThird line", "No conflicts", "One\rTwo"))


def test_conflicts_is_safe_normalized_multiline_body() -> None:
    assert "line one\nline two" in render_page(page(conflicts="line one\r\nline two"))
    with pytest.raises(ValueError, match="reserved"):
        render_page(page(conflicts="## Related\ntrick"))


@pytest.mark.parametrize("changes", [
    {"source_ids": ()}, {"source_ids": ("source-1", "source-1")},
    {"source_ids": ("bad\nsource",)}, {"title": "bad\ntitle"},
    {"page_type": "unknown"}, {"space": "projects:wrong"}, {"space": "project:Bad Slug"},
    {"status": "draft"}, {"updated_at": "yesterday"},
    {"current_state": "## Evidence\ntrick"}, {"timeline_entry": ""},
])
def test_validate_page_rejects_invalid_schema(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_page(page(**changes))


def test_validate_page_reports_missing_source_first() -> None:
    with pytest.raises(ValueError, match="source"):
        validate_page(page(source_ids=(), title="bad\ntitle"))


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    for name in ("20_wiki", "30_answers", "40_index", "90_logs"):
        (tmp_path / name).mkdir(parents=True)
    return tmp_path


def test_stage_rejects_unsafe_or_unmanaged_paths(vault: Path) -> None:
    tx = ChangeTransaction(vault)
    bad = ["../20_wiki/x.md", "/20_wiki/x.md", "20_wiki\\x.md", "20_wiki/a:stream.md",
           "20_wiki/CON.md", "10_raw/x.md", "20_wiki/a\n.md"]
    for path in bad:
        with pytest.raises(ValueError):
            tx.stage(path, "x")
    tx.stage("20_wiki/a.md", "x")
    with pytest.raises(ValueError, match="duplicate"):
        tx.stage("20_wiki/A.md", "y")


def test_publish_rejects_live_case_aliases(vault: Path) -> None:
    (vault / "20_wiki" / "a.md").write_text("old", encoding="utf-8")
    tx = ChangeTransaction(vault)
    tx.stage("20_wiki/A.md", "new")
    with pytest.raises(ValueError, match="case"):
        tx.publish(lambda _: None)
    (vault / "20_wiki" / "Folder").mkdir()
    other = ChangeTransaction(vault)
    other.stage("20_wiki/folder/page.md", "new")
    with pytest.raises(ValueError, match="case"):
        other.publish(lambda _: None)


def test_publish_calls_validator_before_any_live_change(vault: Path) -> None:
    target = vault / "20_wiki" / "page.md"
    target.write_text("old", encoding="utf-8")
    tx = ChangeTransaction(vault)
    tx.stage("20_wiki/page.md", "new")
    with pytest.raises(ValueError, match="invalid"):
        tx.publish(lambda staged: (_ for _ in ()).throw(ValueError("invalid")))
    assert target.read_text(encoding="utf-8") == "old"


def test_publish_rolls_back_existing_and_new_files_on_replace_failure(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = vault / "20_wiki" / "old.md"
    original.write_text("old", encoding="utf-8")
    tx = ChangeTransaction(vault)
    tx.stage("20_wiki/old.md", "updated")
    tx.stage("30_answers/new.md", "new")
    real_replace = os.replace
    calls = 0

    def fail_second(src: str | Path, dst: str | Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("replace failure")
        real_replace(src, dst)

    monkeypatch.setattr("local_kb.transaction.os.replace", fail_second)
    with pytest.raises(OSError, match="replace failure"):
        tx.publish(lambda staged: None)
    assert original.read_text(encoding="utf-8") == "old"
    assert not (vault / "30_answers" / "new.md").exists()
    assert not list(vault.rglob("*.new"))


def test_publish_rolls_back_a_replace_when_its_fsync_fails(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = vault / "20_wiki" / "old.md"
    original.write_text("old", encoding="utf-8")
    tx = ChangeTransaction(vault)
    tx.stage("20_wiki/old.md", "updated")

    monkeypatch.setattr("local_kb.transaction._fsync_file", lambda _: (_ for _ in ()).throw(OSError("fsync failure")))
    with pytest.raises((OSError, RollbackError), match="fsync failure"):
        tx.publish(lambda staged: None)
    assert original.read_text(encoding="utf-8") == "old"
    assert not list(vault.rglob("*.new"))


def test_publish_rollback_restores_metadata_captured_before_reading(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = vault / "20_wiki" / "old.md"
    original.write_text("old", encoding="utf-8")
    original_mtime = 1_700_000_000_000_000_000
    os.utime(original, ns=(original_mtime, original_mtime))
    tx = ChangeTransaction(vault)
    tx.stage("20_wiki/old.md", "updated")
    real_read = Path.read_bytes
    real_fsync = __import__("local_kb.transaction", fromlist=["_fsync_file"])._fsync_file
    failed = False

    def mutating_read(self: Path) -> bytes:
        data = real_read(self)
        if self == original:
            os.utime(self, ns=(original_mtime + 1_000_000_000, original_mtime + 1_000_000_000))
        return data

    def fail_once(path: Path) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("fsync failure")
        real_fsync(path)

    monkeypatch.setattr(Path, "read_bytes", mutating_read)
    monkeypatch.setattr("local_kb.transaction._fsync_file", fail_once)
    with pytest.raises(OSError, match="fsync failure"):
        tx.publish(lambda staged: None)
    assert original.stat().st_mtime_ns == original_mtime


def test_stage_rejects_live_symlink_and_publish_cleans_stage(vault: Path) -> None:
    link = vault / "20_wiki" / "linked"
    try:
        link.symlink_to(vault / "outside", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    tx = ChangeTransaction(vault)
    with pytest.raises(ValueError, match="symlink"):
        tx.stage("20_wiki/linked/x.md", "x")
    tx.stage("40_index/index.md", "index")
    tx.publish(lambda staged: None)
    assert (vault / "40_index/index.md").read_text(encoding="utf-8") == "index"
    assert not tx.stage_root.exists()


def test_concurrent_transactions_serialize_writers(vault: Path) -> None:
    first = ChangeTransaction(vault)
    second = ChangeTransaction(vault)
    first.stage("20_wiki/first.md", "first")
    second.stage("20_wiki/second.md", "second")
    started = threading.Event()
    release = threading.Event()

    def validator(_: tuple[Path, ...]) -> None:
        started.set()
        assert release.wait(3)

    worker = threading.Thread(target=lambda: first.publish(validator))
    worker.start()
    assert started.wait(3)
    done = threading.Event()
    other = threading.Thread(target=lambda: (second.publish(lambda _: None), done.set()))
    other.start()
    assert not done.wait(.15)
    release.set()
    worker.join(3)
    other.join(3)
    assert done.is_set()


def git(vault: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=vault, check=True, text=True,
                          capture_output=True).stdout


def test_commit_git_commits_only_managed_files_and_preserves_unrelated_stage(vault: Path) -> None:
    (vault / "20_wiki" / "page.md").write_text("one", encoding="utf-8")
    other = vault / "notes.md"
    other.write_text("private", encoding="utf-8")
    git(vault, "init")
    git(vault, "add", "notes.md")
    tx = ChangeTransaction(vault)
    assert tx.commit_git("first wiki")
    assert "20_wiki/page.md" in git(vault, "show", "--name-only", "--format=", "HEAD")
    assert "notes.md" not in git(vault, "show", "--name-only", "--format=", "HEAD")
    assert "notes.md" in git(vault, "diff", "--cached", "--name-only")
    (vault / "20_wiki" / "page.md").write_text("two", encoding="utf-8")
    assert tx.commit_git("second wiki")
    assert git(vault, "rev-list", "--count", "HEAD").strip() == "2"
    assert not tx.commit_git("nothing changed")


def test_commit_git_rejects_message_and_surfaces_git_failure(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tx = ChangeTransaction(vault)
    with pytest.raises(ValueError):
        tx.commit_git("bad\nmessage")
    monkeypatch.setattr("local_kb.transaction.subprocess.run", lambda *a, **kw: (_ for _ in ()).throw(OSError("git lost")))
    with pytest.raises(RuntimeError, match="git"):
        tx.commit_git("ok")
