import json
import multiprocessing
import os
from pathlib import Path

import pytest


def _record_failure(queue_root, job_id):
    from local_kb.queue import DiskQueue

    DiskQueue(queue_root, max_retries=20).fail(job_id, RuntimeError("worker"))


def make_vault(tmp_path):
    from local_kb.cli import build_vault

    return build_vault(tmp_path / "vault")


def make_service(tmp_path, registry=None):
    from local_kb.catalog import Catalog
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    vault = make_vault(tmp_path)
    queue = DiskQueue(vault.queue)
    catalog = Catalog(vault.index / "catalog.sqlite3")
    return vault, queue, catalog, IngestService(vault, queue, catalog, registry=registry)


def test_disk_queue_round_trips_independent_metadata_and_retries(tmp_path):
    from local_kb.queue import DiskQueue

    queue = DiskQueue(tmp_path / "queue", max_retries=3)
    job = queue.enqueue(tmp_path / "incoming.txt", job_id="job-one")
    job.metadata["changed"] = "only-memory"

    assert queue.get("job-one").metadata == {}
    assert queue.fail("job-one", RuntimeError("first")).state == "retrying"
    assert queue.fail("job-one", RuntimeError("second")).attempts == 2
    final = queue.fail("job-one", RuntimeError("third"))
    assert (final.attempts, final.state, final.error) == (3, "pending_attention", "third")


def test_disk_queue_rejects_traversal_and_corrupt_json(tmp_path):
    from local_kb.queue import DiskQueue

    queue = DiskQueue(tmp_path / "queue")
    with pytest.raises(ValueError, match="job_id"):
        queue.get("../outside")
    (tmp_path / "queue" / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt"):
        queue.get("bad")


def test_disk_queue_atomic_update_leaves_complete_json_after_replace_failure(tmp_path, monkeypatch):
    from local_kb.queue import DiskQueue

    queue = DiskQueue(tmp_path / "queue")
    queue.enqueue("a.txt", job_id="job")
    before = (tmp_path / "queue" / "job.json").read_text(encoding="utf-8")

    def explode(source, target):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError, match="replace failed"):
        queue.update("job", lambda current: current)
    assert json.loads((tmp_path / "queue" / "job.json").read_text(encoding="utf-8")) == json.loads(before)
    assert not list((tmp_path / "queue").glob("*.tmp"))


def test_disk_queue_does_not_lose_concurrent_process_failure_attempts(tmp_path):
    from local_kb.queue import DiskQueue

    queue = DiskQueue(tmp_path / "queue", max_retries=20)
    queue.enqueue("a.txt", job_id="job")
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_record_failure, args=(queue.root, "job")) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
        assert worker.exitcode == 0
    assert queue.get("job").attempts == 4


def test_stable_tracker_requires_two_matching_observations_and_elapsed_time(tmp_path):
    from local_kb.watcher import StableTracker

    now = [0.0]
    tracker = StableTracker(2.0, clock=lambda: now[0])
    source = tmp_path / "note.txt"
    source.write_text("one", encoding="utf-8")

    assert tracker.observe(source) is False
    now[0] = 1.0
    assert tracker.observe(source) is False
    now[0] = 2.0
    assert tracker.observe(source) is True
    source.write_text("changed", encoding="utf-8")
    now[0] = 3.0
    assert tracker.observe(source) is False


def test_stable_tracker_zero_still_needs_two_observations_and_ignores_unsafe_paths(tmp_path):
    from local_kb.watcher import StableTracker

    tracker = StableTracker(0)
    source = tmp_path / "note.txt"
    source.write_text("x", encoding="utf-8")
    assert tracker.observe(source) is False
    assert tracker.observe(source) is True
    assert tracker.observe(tmp_path) is False
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert tracker.observe(link) is False
    source.unlink()
    assert tracker.observe(source) is False


def test_ingest_text_archives_indexes_caches_and_moves_inbox_file(tmp_path):
    vault, queue, catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "note.txt"
    incoming.write_text("first line\nsecond line\n", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="note-job")

    source = service.process(job.job_id, space="work")

    assert source.status == "extracted"
    assert (vault.root / source.relative_path).read_text(encoding="utf-8") == "first line\nsecond line\n"
    cache = json.loads((vault.index / "cache" / f"{source.version_id}.json").read_text(encoding="utf-8"))
    assert cache["source"]["version_id"] == source.version_id
    assert cache["fragments"][0]["locator"] == "lines:1-1"
    assert catalog.search("second", {"work"})[0].locator == "lines:2-2"
    completed = queue.get(job.job_id)
    assert completed.metadata["processed_path"].endswith("note.txt")
    assert (vault.root / completed.metadata["processed_path"]).is_file()


def test_ingest_pending_format_never_invents_fragments(tmp_path):
    vault, queue, catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "movie.mp4"
    incoming.write_bytes(b"not a movie")
    job = queue.enqueue(incoming, job_id="movie-job")

    source = service.process(job.job_id)

    assert source.status == "pending_extractor"
    assert json.loads((vault.index / "cache" / f"{source.version_id}.json").read_text())["fragments"] == []
    assert catalog.search("not", {"unclassified"}) == []


def test_changed_same_name_keeps_previous_raw_version_and_retry_is_idempotent(tmp_path):
    vault, queue, catalog, service = make_service(tmp_path)
    first = vault.inbox / "note.txt"
    first.write_text("one", encoding="utf-8")
    first_job = queue.enqueue(first, job_id="one")
    old = service.process(first_job.job_id, space="work")
    second = vault.inbox / "note.txt"
    second.write_text("two", encoding="utf-8")
    second_job = queue.enqueue(second, job_id="two")
    newest = service.process(second_job.job_id, space="work")

    assert newest.previous_version_id == old.version_id
    assert (vault.root / old.relative_path).is_file()
    assert service.process(second_job.job_id, space="work").version_id == newest.version_id
    assert catalog.latest_source("work", "note.txt").version_id == newest.version_id


def test_ingest_does_not_overwrite_a_different_processed_file(tmp_path):
    vault, queue, _catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "note.txt"
    incoming.write_text("fresh", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="job")
    target = vault.trash / "processed-inbox" / "job" / "note.txt"
    target.parent.mkdir(parents=True)
    target.write_text("other", encoding="utf-8")

    with pytest.raises(FileExistsError, match="different"):
        service.process(job.job_id)
    assert target.read_text(encoding="utf-8") == "other"
    assert queue.get(job.job_id).state == "retrying"


@pytest.mark.parametrize("stage", ["extract", "catalog", "cache", "move"])
def test_ingest_retry_recovers_after_each_recoverable_stage_failure(tmp_path, monkeypatch, stage):
    vault, queue, catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "recover.txt"
    incoming.write_text("recover me", encoding="utf-8")
    job = queue.enqueue(incoming, job_id=f"retry-{stage}")
    original_extract = service.registry.extract
    original_upsert = catalog.upsert_source
    original_cache = service._write_cache
    import local_kb.ingest as ingest_module

    original_move = ingest_module.shutil.move
    failed = [False]

    def once_then(call):
        def wrapped(*args, **kwargs):
            if not failed[0]:
                failed[0] = True
                raise OSError(stage)
            return call(*args, **kwargs)
        return wrapped

    if stage == "extract":
        monkeypatch.setattr(service.registry, "extract", once_then(original_extract))
    elif stage == "catalog":
        monkeypatch.setattr(catalog, "upsert_source", once_then(original_upsert))
    elif stage == "cache":
        monkeypatch.setattr(service, "_write_cache", once_then(original_cache))
    else:
        monkeypatch.setattr(ingest_module.shutil, "move", once_then(original_move))

    with pytest.raises(OSError, match=stage):
        service.process(job.job_id)
    assert queue.get(job.job_id).state == "retrying"
    completed = service.process(job.job_id)
    assert completed.status == "extracted"
    assert (vault.index / "cache" / f"{completed.version_id}.json").is_file()
    assert (vault.root / queue.get(job.job_id).metadata["processed_path"]).is_file()


def test_cli_ingest_once_prints_version_and_watch_once_gates_unseen_stable_file(tmp_path, capsys):
    from local_kb.cli import main, watch_once
    from local_kb.queue import DiskQueue
    from local_kb.watcher import StableTracker

    vault = make_vault(tmp_path)
    source = vault.inbox / "cli.txt"
    source.write_text("hello", encoding="utf-8")
    assert main(["ingest-once", str(vault.root), str(source), "--space", "work"]) == 0
    assert "ver_" in capsys.readouterr().out
    later = vault.inbox / "later.txt"
    later.write_text("later", encoding="utf-8")
    tracker = StableTracker(0)
    submitted = set()
    assert watch_once(vault, tracker, submitted) == []
    results = watch_once(vault, tracker, submitted)
    assert len(results) == 1
    assert watch_once(vault, tracker, submitted) == []
