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

    cache = vault.index / "cache" / f"{source.version_id}.json"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["fragments"] = [{"locator": "paragraph:1", "text": "forged cache text"}]
    cache.write_text(json.dumps(payload), encoding="utf-8")
    rebuild_catalog(vault)
    assert not Catalog(db).search("forged", {"work"})
    assert Catalog(db).search("searchable", {"work"})

    cache.unlink()
    db.unlink()
    assert rebuild_catalog(vault) == 1
    assert Catalog(db).search("searchable", {"work"})


def test_rebuild_rejects_untrusted_cache_and_raw_metadata(tmp_path):
    from local_kb.health import rebuild_catalog

    vault = _vault(tmp_path)
    source = _seed_cache(vault)
    cache = vault.index / "cache" / f"{source.version_id}.json"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["source"]["relative_path"] = "../outside"
    cache.write_text(json.dumps(payload), encoding="utf-8")
    assert rebuild_catalog(vault) == 1

    cache.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    try:
        cache.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks unavailable")
    assert rebuild_catalog(vault) == 1
    assert outside.read_text(encoding="utf-8") == "{}"


def test_change_transaction_uses_outer_writer_lock_without_nested_deadlock(tmp_path):
    from local_kb.queue import WriterLock
    from local_kb.transaction import ChangeTransaction

    vault = _vault(tmp_path)
    transaction = ChangeTransaction(vault.root, lock_timeout=0)
    transaction.stage("20_wiki/work/locked.md", "safe\n")
    with WriterLock(vault.runtime / "write.lock", timeout=0):
        transaction.publish(lambda _paths: None)
    assert (vault.wiki / "work" / "locked.md").read_text(encoding="utf-8") == "safe\n"


def test_change_transaction_and_writer_lock_contend_on_the_same_kernel_lock(tmp_path):
    import queue
    import threading

    from local_kb.queue import WriterLock
    from local_kb.transaction import ChangeTransaction

    vault = _vault(tmp_path)
    result = queue.Queue()

    def compete():
        try:
            with WriterLock(vault.runtime / "write.lock", timeout=0):
                result.put("entered")
        except Exception as error:
            result.put(error)

    with ChangeTransaction(vault.root, lock_timeout=0)._writer_lock():
        thread = threading.Thread(target=compete)
        thread.start()
        thread.join(timeout=5)
    outcome = result.get_nowait()
    assert isinstance(outcome, TimeoutError)


def test_watch_iteration_respects_writer_lock_before_mutating(tmp_path):
    from local_kb.cli import watch_once
    from local_kb.queue import WriterLock
    from local_kb.watcher import StableTracker

    vault = _vault(tmp_path)
    incoming = vault.inbox / "blocked.txt"
    incoming.write_text("blocked", encoding="utf-8")
    tracker = StableTracker(0, trusted_root=vault.inbox)
    with WriterLock(vault.runtime / "write.lock"):
        with pytest.raises(TimeoutError):
            watch_once(vault, tracker, set())
    assert incoming.exists()
    assert not list(vault.queue.glob("*.json"))


def test_prepare_with_missing_catalog_does_not_create_database(tmp_path, capsys):
    from local_kb.cli import main

    vault = _vault(tmp_path)
    database = vault.index / "catalog.sqlite3"
    database.unlink(missing_ok=True)
    output = vault.runtime / "packet.json"
    assert main([
        "prepare", "missing evidence", "--vault", str(vault.root),
        "--space", "work", "--output", str(output),
    ]) == 0
    packet = json.loads(output.read_text(encoding="utf-8"))
    assert packet["status"] == "insufficient_evidence"
    assert packet["reason"] == "catalog_unavailable"
    assert not database.exists()


def test_prepare_opens_existing_catalog_read_only(tmp_path):
    from local_kb.cli import main

    vault = _vault(tmp_path)
    _seed_cache(vault)
    database = vault.index / "catalog.sqlite3"
    before = (database.stat().st_mtime_ns, database.read_bytes())
    assert main([
        "prepare", "searchable", "--vault", str(vault.root),
        "--space", "work", "--output", str(vault.runtime / "readonly.json"),
    ]) == 0
    assert (database.stat().st_mtime_ns, database.read_bytes()) == before
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_lint_reports_complete_wiki_relationship_and_index_issues(tmp_path):
    from local_kb.health import lint

    vault = _vault(tmp_path)
    source = _seed_cache(vault)
    template = (
        "---\nid: \"{id}\"\ntitle: \"{title}\"\naliases:\n  - \"same\"\n"
        "type: \"topic\"\nspace: \"work\"\nstatus: \"{status}\"\n"
        "confidence: \"low\"\nupdated_at: \"2026-01-01T00:00:00Z\"\n"
        "source_ids:\n  - \"{source_id}\"\n---\n\n## Current State\nClaim\n\n"
        "## Evidence\n- {source_id}\n\n## Conflicts and Gaps\nnone\n\n"
        "## Related\n- {related}\n\n## Timeline\nCreated\n"
    )
    (vault.wiki / "work" / "one.md").write_text(
        template.format(id="duplicate", title="One", status="active",
                        source_id=source.source_id, related="missing-page"),
        encoding="utf-8",
    )
    (vault.wiki / "work" / "two.md").write_text(
        template.format(id="duplicate", title="Two", status="stale",
                        source_id=source.source_id, related="missing-page"),
        encoding="utf-8",
    )
    report = lint(vault)
    assert report["healthy"] is False
    issues = report["issues"]
    assert issues["duplicate_page_ids"]
    assert issues["duplicate_aliases"]
    assert issues["broken_related"]
    assert issues["orphan_pages"]
    assert issues["stale_pages"]

    database = vault.index / "catalog.sqlite3"
    database.unlink()
    report = lint(vault)
    assert report["issues"]["index_raw_mismatches"]


def test_lint_reports_cross_kind_identity_collision_and_ambiguous_related(tmp_path):
    from local_kb.health import lint

    vault = _vault(tmp_path)
    source = _seed_cache(vault)
    template = (
        "---\nid: \"{id}\"\ntitle: \"{title}\"\naliases:\n{aliases}"
        "type: \"topic\"\nspace: \"work\"\nstatus: \"active\"\n"
        "confidence: \"low\"\nupdated_at: \"2026-01-01T00:00:00Z\"\n"
        "source_ids:\n  - \"{source_id}\"\n---\n\n## Current State\nClaim\n\n"
        "## Evidence\n- {source_id}\n\n## Conflicts and Gaps\nnone\n\n"
        "## Related\n- {related}\n\n## Timeline\nCreated\n"
    )
    (vault.wiki / "work" / "a.md").write_text(
        template.format(
            id="alpha", title="Alpha", aliases='  - "beta"\n',
            source_id=source.source_id, related="beta",
        ),
        encoding="utf-8",
    )
    (vault.wiki / "work" / "b.md").write_text(
        template.format(
            id="beta", title="Beta Page", aliases=" []\n",
            source_id=source.source_id, related="alpha",
        ),
        encoding="utf-8",
    )
    report = lint(vault)
    assert report["healthy"] is False
    assert report["issues"]["identity_collisions"] == [
        {
            "value": "beta",
            "bindings": [
                {"kind": "alias", "page": "20_wiki/work/a.md"},
                {"kind": "id", "page": "20_wiki/work/b.md"},
            ],
        }
    ]
    assert {
        "page": "20_wiki/work/a.md", "target": "beta", "reason": "ambiguous"
    } in report["issues"]["broken_related"]


def test_lint_detects_forged_catalog_fragments_and_fts_body(tmp_path):
    import sqlite3

    from local_kb.health import lint

    vault = _vault(tmp_path)
    source = _seed_cache(vault)
    database = vault.index / "catalog.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE source_fragments SET text = 'FORGED' WHERE version_id = ?",
            (source.version_id,),
        )
        connection.execute(
            "UPDATE source_fts SET body = 'FORGED' WHERE version_id = ?",
            (source.version_id,),
        )
    report = lint(vault)
    assert report["healthy"] is False
    assert f"fragments:{source.version_id}" in report["issues"]["index_content_mismatches"]


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
