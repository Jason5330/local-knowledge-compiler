import json
import os
from pathlib import Path

import pytest


def _vault(tmp_path):
    from local_kb.cli import build_vault

    return build_vault(tmp_path / "vault")


def _seed_cache(vault, *, text="searchable proof"):
    from local_kb.catalog import Catalog
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    incoming = vault.inbox / "proof.txt"
    incoming.write_text(text, encoding="utf-8")
    queue = DiskQueue(vault.queue)
    source = IngestService(
        vault, queue, Catalog(vault.index / "catalog.sqlite3")
    ).process(queue.enqueue(incoming, job_id="seed").job_id, space="work")
    return source


def test_second_writer_cannot_enter_and_crashed_owner_is_recoverable(tmp_path):
    from local_kb.queue import WriterLock

    lock_path = tmp_path / "runtime" / "write.lock"
    with WriterLock(lock_path):
        with pytest.raises(TimeoutError):
            with WriterLock(lock_path, timeout=0):
                pass

    # The persistent owner record is stale after the kernel releases the lock.
    with WriterLock(lock_path, timeout=0):
        owner = json.loads(lock_path.read_text(encoding="utf-8"))
        assert owner["format"] == "local-kb-writer-lock-v1"
        assert owner["pid"] == os.getpid()
        assert isinstance(owner["token"], str) and len(owner["token"]) == 32


def test_writer_lock_rejects_foreign_file_and_does_not_delete_replacement(tmp_path):
    from local_kb.queue import WriterLock

    lock_path = tmp_path / "runtime" / "write.lock"
    lock_path.parent.mkdir()
    lock_path.write_text("belongs to another application", encoding="utf-8")
    with pytest.raises(ValueError, match="not a local knowledge"):
        with WriterLock(lock_path, timeout=0):
            pass
    assert lock_path.read_text(encoding="utf-8") == "belongs to another application"

    lock_path.unlink()
    held = WriterLock(lock_path)
    held.__enter__()
    original = lock_path.read_bytes()
    moved = lock_path.with_suffix(".old")
    if os.name == "nt":
        held.__exit__(None, None, None)
        pytest.skip("Windows correctly prevents replacing the open lock")
    lock_path.rename(moved)
    lock_path.write_text("replacement", encoding="utf-8")
    held.__exit__(None, None, None)
    assert lock_path.read_text(encoding="utf-8") == "replacement"
    assert moved.read_bytes() == original


def test_writer_lock_rejects_symlink_without_touching_target(tmp_path):
    from local_kb.queue import WriterLock

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("keep", encoding="utf-8")
    try:
        (runtime / "write.lock").symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks unavailable")
    with pytest.raises(ValueError, match="unsafe"):
        with WriterLock(runtime / "write.lock", timeout=0):
            pass
    assert outside.read_text(encoding="utf-8") == "keep"


def test_rebuild_restores_search_from_cache_without_deleting_good_db_on_failure(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.health import rebuild_catalog

    vault = _vault(tmp_path)
    source = _seed_cache(vault)
    db = vault.index / "catalog.sqlite3"
    db.unlink()
    count = rebuild_catalog(vault)
    assert count == 1
    assert Catalog(db).search("searchable", {"work"})[0].version_id == source.version_id

    before = db.read_bytes()
    (vault.index / "cache" / f"{source.version_id}.json").write_text(
        "{broken", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="cache"):
        rebuild_catalog(vault)
    assert db.read_bytes() == before


def test_rebuild_rejects_untrusted_cache_and_raw_metadata(tmp_path):
    from local_kb.health import rebuild_catalog

    vault = _vault(tmp_path)
    source = _seed_cache(vault)
    cache = vault.index / "cache" / f"{source.version_id}.json"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["source"]["relative_path"] = "../outside"
    cache.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="cache"):
        rebuild_catalog(vault)

    cache.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    try:
        cache.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks unavailable")
    with pytest.raises(ValueError, match="cache"):
        rebuild_catalog(vault)
    assert outside.read_text(encoding="utf-8") == "{}"


def test_rebuild_respects_the_single_writer_lock(tmp_path):
    from local_kb.health import rebuild_catalog
    from local_kb.queue import WriterLock

    vault = _vault(tmp_path)
    _seed_cache(vault)
    database = vault.index / "catalog.sqlite3"
    before = database.read_bytes()
    with WriterLock(vault.runtime / "write.lock"):
        with pytest.raises(TimeoutError, match="writer lock"):
            rebuild_catalog(vault)
    assert database.read_bytes() == before


def test_mutating_cli_respects_the_single_writer_lock(tmp_path, capsys):
    from local_kb.cli import main
    from local_kb.queue import WriterLock

    vault = _vault(tmp_path)
    incoming = vault.inbox / "blocked.txt"
    incoming.write_text("must remain untouched", encoding="utf-8")
    with WriterLock(vault.runtime / "write.lock"):
        assert (
            main(
                [
                    "ingest-once",
                    str(vault.root),
                    str(incoming),
                    "--space",
                    "work",
                ]
            )
            == 1
        )
    assert "writer lock unavailable" in capsys.readouterr().err
    assert incoming.read_text(encoding="utf-8") == "must remain untouched"
    assert not list(vault.queue.glob("*.json"))


def test_lint_is_read_only_and_reports_wiki_provenance_and_pending(tmp_path):
    from local_kb.health import lint
    from local_kb.queue import DiskQueue

    vault = _vault(tmp_path)
    source = _seed_cache(vault)
    page = vault.wiki / "work" / "bad.md"
    page.write_text(
        "---\nid: \"bad\"\ntitle: \"Bad\"\naliases: []\ntype: \"topic\"\n"
        "space: \"work\"\nstatus: \"active\"\nconfidence: \"low\"\n"
        "updated_at: \"2026-01-01T00:00:00Z\"\nsource_ids:\n"
        "  - \"missing-source\"\n---\n\n## Current State\nClaim\n\n"
        "## Evidence\n- missing-source\n\n## Conflicts and Gaps\n無\n\n"
        "## Related\n無\n\n## Timeline\nCreated\n",
        encoding="utf-8",
    )
    DiskQueue(vault.queue).enqueue(vault.inbox / "later.txt", job_id="pending")
    before = {
        path.relative_to(vault.root).as_posix(): (path.stat().st_mtime_ns, path.read_bytes())
        for path in vault.root.rglob("*")
        if path.is_file()
    }
    report = lint(vault)
    after = {
        path.relative_to(vault.root).as_posix(): (path.stat().st_mtime_ns, path.read_bytes())
        for path in vault.root.rglob("*")
        if path.is_file()
    }
    assert report["healthy"] is False
    assert report["wiki_pages"] == 1
    assert report["pending_jobs"] == 1
    assert report["missing_source_pages"] == ["20_wiki/work/bad.md"]
    assert source.source_id in report["catalog_source_ids"]
    assert after == before


def test_lint_and_rebuild_cli_print_json_and_count(tmp_path, capsys):
    from local_kb.cli import main

    vault = _vault(tmp_path)
    _seed_cache(vault)
    assert main(["lint", "--vault", str(vault.root)]) == 0
    assert json.loads(capsys.readouterr().out)["healthy"] is True
    assert main(["rebuild", "--vault", str(vault.root)]) == 0
    assert capsys.readouterr().out.strip() == "Indexed sources: 1"
