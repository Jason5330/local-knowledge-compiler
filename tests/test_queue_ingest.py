import json
import errno
import multiprocessing
import os
from pathlib import Path
import subprocess

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


def test_disk_queue_rejects_unknown_state_and_bad_field_types(tmp_path):
    from local_kb.queue import DiskQueue

    queue = DiskQueue(tmp_path / "queue")
    queue.enqueue("a.txt", job_id="job")
    path = tmp_path / "queue" / "job.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["state"] = "invented"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt"):
        queue.get("job")


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


def test_disk_queue_lists_valid_jobs_stably_and_finds_active_source(tmp_path):
    from local_kb.queue import DiskQueue

    queue = DiskQueue(tmp_path / "queue")
    queue.enqueue("C:/inbox/b.txt", job_id="b")
    queue.enqueue("C:/inbox/a.txt", job_id="a")
    assert [job.job_id for job in queue.iter_jobs()] == ["a", "b"]
    assert queue.active_for_source("C:/inbox/a.txt").job_id == "a"


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


def test_stable_tracker_trusted_root_accepts_only_direct_safe_children(tmp_path):
    from local_kb.watcher import StableTracker

    inbox = tmp_path / "00_inbox"
    inbox.mkdir()
    direct = inbox / "direct.txt"
    direct.write_text("safe", encoding="utf-8")
    nested = inbox / "nested"
    nested.mkdir()
    child = nested / "not-direct.txt"
    child.write_text("no", encoding="utf-8")
    tracker = StableTracker(0, trusted_root=inbox)

    assert tracker.observe(direct) is False
    assert tracker.observe(direct) is True
    assert tracker.observe(child) is False
    tracker.forget(direct)
    assert tracker.observe(direct) is False


def test_stable_tracker_resets_when_same_signature_has_new_identity(tmp_path):
    from local_kb.watcher import StableTracker

    source = tmp_path / "same.txt"
    source.write_text("one", encoding="utf-8")
    timestamp = source.stat().st_mtime_ns
    tracker = StableTracker(0)
    assert tracker.observe(source) is False
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("two", encoding="utf-8")
    os.utime(replacement, ns=(timestamp, timestamp))
    os.replace(replacement, source)
    assert tracker.observe(source) is False


def test_stable_tracker_rejects_symlinked_trusted_root_and_parent(tmp_path):
    from local_kb.watcher import StableTracker

    real = tmp_path / "real"
    real.mkdir()
    (real / "note.txt").write_text("safe", encoding="utf-8")
    (real / "inbox").mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("links unavailable")
    tracker = StableTracker(0, trusted_root=linked)
    assert tracker.observe(linked / "note.txt") is False


def test_stable_tracker_rejects_windows_junction_root_and_parent(tmp_path):
    from local_kb.watcher import StableTracker

    real = tmp_path / "real"
    real.mkdir()
    (real / "note.txt").write_text("safe", encoding="utf-8")
    junction = tmp_path / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(real)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        pytest.skip("junctions unavailable")
    with pytest.raises(ValueError, match="safe"):
        StableTracker(0, trusted_root=junction)
    with pytest.raises(ValueError, match="safe"):
        StableTracker(0, trusted_root=junction / "inbox")


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


def test_ingest_claim_prevents_new_same_name_from_being_deleted(tmp_path, monkeypatch):
    vault, queue, _catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "claim.txt"
    incoming.write_text("OLD", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="claim")
    original_archive = service._archive

    def replace_name_after_claim(claimed_job, space):
        assert Path(claimed_job.source_path).parent == vault.staging / job.job_id
        incoming.write_text("NEW", encoding="utf-8")
        return original_archive(claimed_job, space)

    monkeypatch.setattr(service, "_archive", replace_name_after_claim)
    service.process(job.job_id)
    assert incoming.read_text(encoding="utf-8") == "NEW"
    completed = queue.get(job.job_id)
    assert completed.metadata["original_source_path"] == str(incoming.resolve())


def test_processed_move_before_metadata_failure_recovers_on_retry(tmp_path, monkeypatch):
    vault, queue, catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "transition.txt"
    incoming.write_text("transition evidence", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="transition")
    original_mark = service._mark
    failed = [False]

    def fail_validated_once(job_id, state, **metadata):
        if state == "validated" and not failed[0]:
            failed[0] = True
            raise OSError("transition")
        return original_mark(job_id, state, **metadata)

    monkeypatch.setattr(service, "_mark", fail_validated_once)
    with pytest.raises(OSError, match="transition"):
        service.process(job.job_id)
    assert catalog.search("transition", {"unclassified"}) == []
    completed = service.process(job.job_id)
    restored = queue.get(job.job_id)
    assert completed.status == "extracted"
    assert restored.state == "pending_attention"
    assert restored.metadata["compiler_status"] == "needs_agent"
    assert len(list((vault.runtime / "manual").glob("manual_*.json"))) == 1
    assert (vault.root / restored.metadata["processed_path"]).is_file()
    assert catalog.search("transition", {"unclassified"})


def test_cross_volume_preserved_original_is_a_tombstone_until_changed(tmp_path, monkeypatch):
    from local_kb.cli import watch_once
    from local_kb.watcher import StableTracker

    vault, queue, _catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "cross.txt"
    incoming.write_text("old", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="cross")
    monkeypatch.setattr(
        service,
        "_atomic_claim_no_replace",
        lambda *_: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross volume")),
    )
    service.process(job.job_id)
    assert incoming.exists()
    tracker = StableTracker(0, trusted_root=vault.inbox)
    submitted = set()
    for _ in range(4):
        assert watch_once(vault, tracker, submitted) == []
    assert len(queue.iter_jobs()) == 1
    incoming.write_text("new", encoding="utf-8")
    assert watch_once(vault, tracker, submitted) == []
    assert len(watch_once(vault, tracker, submitted)) == 1
    assert len(queue.iter_jobs()) == 2


def test_cross_volume_retry_never_unlinks_replacement_at_original_path(tmp_path, monkeypatch):
    vault, queue, _catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "preserved.txt"
    incoming.write_text("same bytes", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="preserved")
    monkeypatch.setattr(
        service,
        "_atomic_claim_no_replace",
        lambda *_: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross volume")),
    )
    original_cache = service._write_cache
    failed = [False]

    def fail_cache_once(source, extraction):
        if not failed[0]:
            failed[0] = True
            raise OSError("cache")
        return original_cache(source, extraction)

    monkeypatch.setattr(service, "_write_cache", fail_cache_once)
    with pytest.raises(OSError, match="cache"):
        service.process(job.job_id)
    replacement = vault.inbox / "replacement.txt"
    replacement.write_text("same bytes", encoding="utf-8")
    os.replace(replacement, incoming)
    replacement_identity = incoming.stat().st_ino
    service.process(job.job_id)
    assert incoming.is_file()
    assert incoming.stat().st_ino == replacement_identity


def test_pending_attention_blocks_same_identity_but_allows_replacement(tmp_path):
    from local_kb.queue import DiskQueue

    source = tmp_path / "pending.txt"
    source.write_text("old", encoding="utf-8")
    queue = DiskQueue(tmp_path / "queue", max_retries=3)
    job = queue.enqueue(source, job_id="pending")
    for _ in range(3):
        queue.fail(job.job_id, RuntimeError("fail"))
    assert queue.active_for_source(source).job_id == job.job_id
    replacement = tmp_path / "new.txt"
    replacement.write_text("new", encoding="utf-8")
    os.replace(replacement, source)
    assert queue.active_for_source(source) is None


def test_windows_claim_handle_blocks_path_replacement_before_rename(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows handle semantics")
    import local_kb.source_store as source_store

    vault, queue, _catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "pinned.txt"
    incoming.write_text("OLD", encoding="utf-8")
    replacement = vault.inbox / "replacement.txt"
    replacement.write_text("NEW", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="pinned")
    original_rename = source_store._windows_rename_directory_handle

    def try_replace_while_handle_is_held(handle, target):
        if ".kb" in Path(target).parts and "staging" in Path(target).parts:
            with pytest.raises(OSError):
                os.replace(replacement, incoming)
        return original_rename(handle, target)

    monkeypatch.setattr(
        source_store, "_windows_rename_directory_handle", try_replace_while_handle_is_held
    )
    service.process(job.job_id)
    assert replacement.read_text(encoding="utf-8") == "NEW"


def test_ingest_pending_format_never_invents_fragments(tmp_path):
    vault, queue, catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "movie.mp4"
    incoming.write_bytes(b"not a movie")
    job = queue.enqueue(incoming, job_id="movie-job")

    source = service.process(job.job_id)

    assert source.status == "pending_extractor"
    assert json.loads((vault.index / "cache" / f"{source.version_id}.json").read_text())["fragments"] == []
    assert catalog.search("not", {"unclassified"}) == []


def test_ingest_rejects_pending_extractor_with_invented_fragments(tmp_path):
    from local_kb.extractors.base import Extraction, Fragment

    class BadRegistry:
        def extract(self, _path):
            return Extraction("pending_extractor", [Fragment("invented", "no")])

    vault, queue, _catalog, service = make_service(tmp_path, registry=BadRegistry())
    incoming = vault.inbox / "bad.mp4"
    incoming.write_bytes(b"x")
    job = queue.enqueue(incoming, job_id="bad-pending")

    with pytest.raises(ValueError, match="pending_extractor"):
        service.process(job.job_id)
    assert queue.get(job.job_id).state == "retrying"


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


def test_processed_same_bytes_is_recovered_without_leaving_incoming_for_watch(tmp_path):
    vault, queue, _catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "same.txt"
    incoming.write_text("same", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="same")
    target = vault.trash / "processed-inbox" / "same" / "same.txt"
    target.parent.mkdir(parents=True)
    target.write_text("same", encoding="utf-8")

    service.process(job.job_id)
    completed = queue.get(job.job_id)
    actual = vault.root / completed.metadata["processed_path"]
    assert not incoming.exists()
    assert actual.is_file()
    assert actual == target
    assert actual.read_text(encoding="utf-8") == "same"


def test_processed_job_junction_is_rejected_without_writing_outside_vault(tmp_path):
    vault, queue, _catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "junction.txt"
    incoming.write_text("safe", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="junction-job")
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = vault.trash / "processed-inbox" / job.job_id
    junction.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        pytest.skip("junctions unavailable")

    with pytest.raises((OSError, ValueError), match="link|junction|safe"):
        service.process(job.job_id)
    assert not (outside / "junction.txt").exists()
    failed = queue.get(job.job_id)
    assert Path(failed.metadata["claimed_path"]).is_file()
    assert queue.get(job.job_id).state == "retrying"


def test_processed_publish_rehashes_incoming_after_archive_before_removal(tmp_path, monkeypatch):
    vault, queue, catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "changed-after-archive.txt"
    incoming.write_text("A", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="changed-after-archive")
    original_cache = service._write_cache

    def change_then_cache(source, extraction):
        incoming.write_text("B", encoding="utf-8")
        original_cache(source, extraction)

    monkeypatch.setattr(service, "_write_cache", change_then_cache)
    service.process(job.job_id)
    assert incoming.read_text(encoding="utf-8") == "B"
    assert (vault.trash / "processed-inbox" / job.job_id / incoming.name).read_text() == "A"
    assert catalog.latest_source("unclassified", incoming.name) is not None
    completed = queue.get(job.job_id)
    assert completed.state == "pending_attention"
    assert completed.metadata["compiler_status"] == "needs_agent"


def test_ingest_rejects_untrusted_resume_metadata(tmp_path):
    from dataclasses import asdict
    from local_kb.models import SourceVersion

    vault, queue, _catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "resume.txt"
    incoming.write_text("safe", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="resume")
    forged = SourceVersion(
        source_id="src_aaaaaaaaaaaaaaaa",
        version_id="ver_" + "a" * 64,
        space="work",
        original_name="resume.txt",
        relative_path="10_raw/work/src_aaaaaaaaaaaaaaaa/ver_" + "a" * 64 + "/resume.txt",
        sha256="a" * 64,
        media_type="text/plain",
        status="archived",
    )
    queue.update(job.job_id, lambda current: current.metadata.update(source=asdict(forged)))

    with pytest.raises(ValueError, match="resume"):
        service.process(job.job_id)
    assert queue.get(job.job_id).state == "retrying"


@pytest.mark.parametrize("damage", ["manifest", "payload"])
def test_ingest_resume_revalidates_manifest_and_raw_hash(tmp_path, damage):
    vault, queue, _catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "verify.txt"
    incoming.write_text("original", encoding="utf-8")
    job = queue.enqueue(incoming, job_id=f"verify-{damage}")
    source = service.process(job.job_id)
    raw = vault.root / source.relative_path
    if damage == "manifest":
        (raw.parent / "manifest.json").write_text("{bad", encoding="utf-8")
    else:
        raw.write_text("modified", encoding="utf-8")

    with pytest.raises(ValueError, match="resume"):
        service.process(job.job_id)
    assert queue.get(job.job_id).state == "retrying"


def test_catalog_initialization_failure_is_recorded_on_existing_job(tmp_path, monkeypatch):
    vault, queue, catalog, service = make_service(tmp_path)
    incoming = vault.inbox / "init.txt"
    incoming.write_text("x", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="init")
    monkeypatch.setattr(catalog, "initialize", lambda: (_ for _ in ()).throw(OSError("init")))

    with pytest.raises(OSError, match="init"):
        service.process(job.job_id)
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

    original_move = ingest_module.os.link
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
        monkeypatch.setattr(ingest_module.os, "link", once_then(original_move))

    with pytest.raises(OSError, match=stage):
        service.process(job.job_id)
    assert queue.get(job.job_id).state == "retrying"
    if stage in {"cache", "move"}:
        assert catalog.search("recover", {"unclassified"}) == []
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


def test_watch_reingests_a_new_file_with_the_same_name_without_growing_submitted(tmp_path):
    from local_kb.cli import watch_once
    from local_kb.watcher import StableTracker

    vault = make_vault(tmp_path)
    tracker = StableTracker(0, trusted_root=vault.inbox)
    submitted = set()
    same = vault.inbox / "same-name.txt"
    same.write_text("first", encoding="utf-8")
    assert watch_once(vault, tracker, submitted) == []
    assert len(watch_once(vault, tracker, submitted)) == 1
    assert submitted == set()
    same.write_text("second", encoding="utf-8")
    assert watch_once(vault, tracker, submitted) == []
    assert len(watch_once(vault, tracker, submitted)) == 1
    assert submitted == set()
