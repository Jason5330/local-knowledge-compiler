import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def write_file(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_file_sha256_streams_the_file_contents(tmp_path: Path) -> None:
    from local_kb.source_store import file_sha256

    incoming = write_file(tmp_path / "large.bin", b"abc" * 100_000)

    assert file_sha256(incoming) == hashlib.sha256(incoming.read_bytes()).hexdigest()


def test_archive_stores_an_immutable_source_version(tmp_path: Path) -> None:
    from local_kb.source_store import SourceStore

    raw_root = tmp_path / "10_raw"
    incoming = write_file(tmp_path / "report.md", b"hello source")

    archived = SourceStore(raw_root).archive(incoming, "work")

    assert archived.source_id == f"src_{archived.sha256[:16]}"
    assert archived.version_id.startswith("ver_")
    assert archived.relative_path == (
        f"10_raw/work/{archived.source_id}/{archived.version_id}/report.md"
    )
    assert archived.status == "archived"
    assert archived.media_type == "text/markdown"
    assert (raw_root / "work" / archived.source_id / archived.version_id / "report.md").read_bytes() == b"hello source"


def test_archive_deduplicates_equal_content_with_a_different_name(tmp_path: Path) -> None:
    from local_kb.source_store import SourceStore

    store = SourceStore(tmp_path / "10_raw")
    first = store.archive(write_file(tmp_path / "first.md", b"same bytes"), "work")
    second = store.archive(write_file(tmp_path / "renamed.txt", b"same bytes"), "personal")

    assert second == first
    assert second.original_name == "first.md"


def test_archive_creates_a_version_chain_without_replacing_v1(tmp_path: Path) -> None:
    from local_kb.source_store import SourceStore

    raw_root = tmp_path / "10_raw"
    store = SourceStore(raw_root)
    first = store.archive(write_file(tmp_path / "note.md", b"v1"), "work")
    second = store.archive(
        write_file(tmp_path / "note-new.md", b"v2"),
        "work",
        source_id=first.source_id,
        previous_version_id=first.version_id,
    )

    assert second.source_id == first.source_id
    assert second.previous_version_id == first.version_id
    assert second.version_id != first.version_id
    assert (raw_root / "work" / first.source_id / first.version_id / "note.md").read_bytes() == b"v1"
    assert (raw_root / "work" / second.source_id / second.version_id / "note-new.md").read_bytes() == b"v2"


def test_archive_copies_the_input_instead_of_referencing_it(tmp_path: Path) -> None:
    from local_kb.source_store import SourceStore

    incoming = write_file(tmp_path / "note.txt", b"before")
    archived = SourceStore(tmp_path / "10_raw").archive(incoming, "work")
    incoming.write_bytes(b"after")

    assert (tmp_path / archived.relative_path).read_bytes() == b"before"


@pytest.mark.parametrize("name", ["missing.txt", "directory"])
def test_archive_rejects_non_regular_input(tmp_path: Path, name: str) -> None:
    from local_kb.source_store import SourceStore

    incoming = tmp_path / name
    if name == "directory":
        incoming.mkdir()

    with pytest.raises(ValueError, match="regular file"):
        SourceStore(tmp_path / "10_raw").archive(incoming, "work")


@pytest.mark.parametrize(
    ("space", "source_id"),
    [
        ("../outside", None),
        ("work/child", None),
        ("Work", None),
        ("work.", None),
        ("work ", None),
        ("con", None),
        ("con.txt", None),
        ("work", "src_../outside"),
        ("work", "source-id"),
        ("work", "src_Upper"),
        ("work", "src_work."),
    ],
)
def test_archive_rejects_path_traversal_and_invalid_identifiers(
    tmp_path: Path, space: str, source_id: str | None
) -> None:
    from local_kb.source_store import SourceStore

    with pytest.raises(ValueError, match="invalid"):
        SourceStore(tmp_path / "10_raw").archive(
            write_file(tmp_path / "note.txt", b"safe"), space, source_id=source_id
        )


def test_archive_rejects_a_preexisting_immutable_target(tmp_path: Path) -> None:
    from local_kb.source_store import SourceStore, file_sha256

    raw_root = tmp_path / "10_raw"
    incoming = write_file(tmp_path / "note.txt", b"collision")
    digest = file_sha256(incoming)
    target = raw_root / "work" / "src_collision" / f"ver_{digest}"
    target.mkdir(parents=True)
    (target / "note.txt").write_bytes(b"untrusted")

    with pytest.raises(ValueError, match="manifest"):
        SourceStore(raw_root).archive(incoming, "work", source_id="src_collision")

    assert (target / "note.txt").read_bytes() == b"untrusted"


def test_archive_rejects_corrupt_manifest_instead_of_silently_deduplicating(
    tmp_path: Path,
) -> None:
    from local_kb.source_store import SourceStore

    raw_root = tmp_path / "10_raw"
    store = SourceStore(raw_root)
    archived = store.archive(write_file(tmp_path / "first.txt", b"same"), "work")
    manifest = raw_root / "work" / archived.source_id / archived.version_id / "manifest.json"
    manifest.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest"):
        store.archive(write_file(tmp_path / "again.txt", b"same"), "work")


def test_archive_fails_when_the_copied_checksum_does_not_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_kb import source_store

    incoming = write_file(tmp_path / "note.txt", b"checksum")
    actual = hashlib.sha256(b"checksum").hexdigest()
    calls = 0

    def mismatching_hash(path: Path) -> str:
        nonlocal calls
        calls += 1
        return actual if calls == 1 else "0" * 64

    monkeypatch.setattr(source_store, "file_sha256", mismatching_hash)

    with pytest.raises(ValueError, match="checksum"):
        source_store.SourceStore(tmp_path / "10_raw").archive(incoming, "work")

    assert not list((tmp_path / "10_raw").rglob(".ver_*.tmp-*"))


def test_archive_is_safe_when_equal_content_arrives_concurrently(tmp_path: Path) -> None:
    from local_kb.source_store import SourceStore

    raw_root = tmp_path / "10_raw"
    incoming = write_file(tmp_path / "parallel.bin", b"parallel content")

    def archive_once(_: int):
        return SourceStore(raw_root).archive(incoming, "work", source_id="src_parallel")

    with ThreadPoolExecutor(max_workers=6) as executor:
        versions = list(executor.map(archive_once, range(12)))

    assert len(set(versions)) == 1
    manifests = list(raw_root.rglob("manifest.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text(encoding="utf-8"))["sha256"] == versions[0].sha256


def test_manifest_round_trips_all_source_version_fields(tmp_path: Path) -> None:
    from local_kb.source_store import SourceStore

    raw_root = tmp_path / "10_raw"
    store = SourceStore(raw_root)
    first = store.archive(write_file(tmp_path / "original.name", b"old"), "work")
    archived = store.archive(
        write_file(tmp_path / "new.name", b"new"),
        "work",
        source_id=first.source_id,
        previous_version_id=first.version_id,
    )
    manifest_path = raw_root / "work" / archived.source_id / archived.version_id / "manifest.json"

    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "source_id": archived.source_id,
        "version_id": archived.version_id,
        "space": "work",
        "original_name": "new.name",
        "relative_path": archived.relative_path,
        "sha256": archived.sha256,
        "media_type": archived.media_type,
        "status": "archived",
        "previous_version_id": first.version_id,
        "created_sequence": None,
    }


def test_legacy_manifest_without_created_sequence_round_trips_as_none(
    tmp_path: Path,
) -> None:
    from local_kb.source_store import SourceStore

    raw_root = tmp_path / "10_raw"
    store = SourceStore(raw_root)
    archived = store.archive(write_file(tmp_path / "legacy.txt", b"legacy"), "work")
    manifest_path = raw_root / "work" / archived.source_id / archived.version_id / "manifest.json"
    legacy = json.loads(manifest_path.read_text(encoding="utf-8"))
    del legacy["created_sequence"]
    manifest_path.write_text(json.dumps(legacy), encoding="utf-8")

    restored = store._read_manifest(manifest_path)

    assert restored == archived
    assert restored.created_sequence is None


@pytest.mark.parametrize("change", ["unknown", "missing_required"])
def test_manifest_rejects_unknown_or_missing_required_fields(
    tmp_path: Path, change: str
) -> None:
    from local_kb.source_store import SourceStore

    raw_root = tmp_path / "10_raw"
    store = SourceStore(raw_root)
    archived = store.archive(write_file(tmp_path / "manifest.txt", b"manifest"), "work")
    manifest_path = raw_root / "work" / archived.source_id / archived.version_id / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if change == "unknown":
        data["surprise"] = True
    else:
        del data["status"]
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest"):
        store._read_manifest(manifest_path)


def make_junction(link: Path, target: Path) -> None:
    if os.name != "nt" or not hasattr(Path, "is_junction"):
        pytest.skip("junctions are only available on supported Windows Python")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot create test junction: {result.stderr}")
    assert link.is_junction()


def test_archive_rejects_a_junction_below_raw_root_without_writing_outside(
    tmp_path: Path,
) -> None:
    from local_kb.source_store import SourceStore

    raw_root = tmp_path / "10_raw"
    raw_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    make_junction(raw_root / "work", outside)

    with pytest.raises(ValueError, match="unsafe|outside raw_root"):
        SourceStore(raw_root).archive(write_file(tmp_path / "note.txt", b"safe"), "work")

    assert list(outside.iterdir()) == []


def test_source_store_rejects_a_raw_root_junction(tmp_path: Path) -> None:
    from local_kb.source_store import SourceStore

    outside = tmp_path / "outside"
    outside.mkdir()
    raw_root = tmp_path / "10_raw"
    make_junction(raw_root, outside)

    with pytest.raises(ValueError, match="raw_root"):
        SourceStore(raw_root)


@pytest.mark.parametrize("name", ["manifest.json", "MANIFEST.JSON"])
def test_archive_rejects_reserved_manifest_filename_before_copying(
    tmp_path: Path, name: str
) -> None:
    from local_kb.source_store import SourceStore

    raw_root = tmp_path / "10_raw"
    incoming = write_file(tmp_path / name, b"not metadata")

    with pytest.raises(ValueError, match="reserved.*manifest"):
        SourceStore(raw_root).archive(incoming, "work")

    assert list(raw_root.rglob("*")) == []


@pytest.mark.parametrize("name", ["note<bad>.txt", "note|bad.txt", "note?.txt"])
def test_archive_rejects_windows_forbidden_filename_characters(
    tmp_path: Path, name: str
) -> None:
    from local_kb.source_store import SourceStore

    if os.name == "nt":
        pytest.skip("Windows refuses these names before this portable validation")
    raw_root = tmp_path / "10_raw"
    incoming = write_file(tmp_path / name, b"unsafe name")

    with pytest.raises(ValueError, match="invalid original filename"):
        SourceStore(raw_root).archive(incoming, "work")

    assert not list(raw_root.rglob(".ver_*.tmp-*"))
    assert list(raw_root.rglob("*")) == []


def test_archive_rejects_colon_stream_name_before_publishing(tmp_path: Path) -> None:
    from local_kb.source_store import SourceStore

    raw_root = tmp_path / "10_raw"
    incoming = tmp_path / "note.txt:stream"
    try:
        incoming.write_bytes(b"unsafe stream")
    except OSError as error:
        pytest.skip(f"cannot create portable colon regression input: {error}")

    with pytest.raises(ValueError, match="filename|regular file"):
        SourceStore(raw_root).archive(incoming, "work")

    assert not list(raw_root.rglob(".ver_*.tmp-*"))
    assert list(raw_root.rglob("*")) == []


def test_kernel_lock_is_released_when_lock_holder_is_killed(tmp_path: Path) -> None:
    from local_kb.source_store import SourceStore

    raw_root = tmp_path / "10_raw"
    source_root = Path(__file__).parents[1] / "src"
    script = (
        "import sys,time; "
        f"sys.path.insert(0, {str(source_root)!r}); "
        "from local_kb.source_store import SourceStore; "
        f"store=SourceStore({str(raw_root)!r}); "
        "lock=store._archive_lock(); lock.__enter__(); "
        "print('locked', flush=True); time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        process.kill()
        process.wait(timeout=5)

        started = time.monotonic()
        archived = SourceStore(raw_root).archive(
            write_file(tmp_path / "after-crash.txt", b"released"), "work"
        )

        assert time.monotonic() - started < 3
        assert archived.status == "archived"
        assert (raw_root / ".archive.lock").is_file()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def test_source_directory_cannot_be_replaced_by_junction_during_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("this regression exercises Windows junction replacement")
    from local_kb.source_store import SourceStore

    raw_root = tmp_path / "10_raw"
    outside = tmp_path / "outside"
    outside.mkdir()
    incoming = write_file(tmp_path / "race.txt", b"race payload")
    store = SourceStore(raw_root)
    original_copy = store._copy_with_fsync
    replacement_blocked = False

    def racing_copy(source: Path, destination: Path) -> None:
        nonlocal replacement_blocked
        source_dir = destination.parent.parent
        moved_source = source_dir.with_name(f"{source_dir.name}_moved")
        try:
            source_dir.rename(moved_source)
        except OSError:
            replacement_blocked = True
        else:
            (outside / destination.parent.name).mkdir()
            make_junction(source_dir, outside)
        original_copy(source, destination)

    monkeypatch.setattr(store, "_copy_with_fsync", racing_copy)
    archive_error = None
    try:
        store.archive(incoming, "work")
    except (OSError, ValueError) as error:
        archive_error = error

    assert replacement_blocked or archive_error is not None
    assert not list(outside.rglob("race.txt"))
    assert not list(outside.rglob("manifest.json"))


def test_new_archive_directories_are_synced_in_creation_and_publish_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_kb.source_store import SourceStore

    synced: list[Path] = []

    def record_sync(self: SourceStore, path: Path) -> None:
        synced.append(Path(path))

    monkeypatch.setattr(SourceStore, "_sync_directory", record_sync, raising=False)
    archived = SourceStore(tmp_path / "10_raw").archive(
        write_file(tmp_path / "durable.txt", b"durable"), "work"
    )

    names = [path.name for path in synced]
    stage_index = next(
        index for index, name in enumerate(names) if name.startswith(".ver_")
    )
    assert names.index("10_raw") < names.index("work")
    assert names.index("work") < names.index(archived.source_id)
    assert names.index(archived.source_id) < stage_index
    assert names[-1] == archived.source_id


def test_flush_failure_does_not_publish_or_leave_temp_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_kb.source_store import SourceStore

    raw_root = tmp_path / "10_raw"
    store = SourceStore(raw_root)
    (raw_root / ".archive.lock").write_bytes(b"\0")

    def fail_flush(self: SourceStore, file_object) -> None:
        raise OSError("injected flush failure")

    monkeypatch.setattr(SourceStore, "_flush_file", fail_flush, raising=False)

    with pytest.raises(OSError, match="injected flush"):
        store.archive(write_file(tmp_path / "flush.txt", b"flush"), "work")

    assert not list(raw_root.rglob("manifest.json"))
    assert not list(raw_root.rglob(".ver_*.tmp-*"))


def test_atomic_publish_failure_does_not_return_success_or_leave_temp_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_kb.source_store import SourceStore

    raw_root = tmp_path / "10_raw"

    def fail_publish(
        self: SourceStore, source: Path, target: Path, **handles
    ) -> None:
        raise OSError("injected publish failure")

    monkeypatch.setattr(SourceStore, "_atomic_publish", fail_publish, raising=False)

    with pytest.raises(OSError, match="injected publish"):
        SourceStore(raw_root).archive(
            write_file(tmp_path / "rename.txt", b"rename"), "work"
        )

    assert not list(raw_root.rglob("manifest.json"))
    assert not list(raw_root.rglob(".ver_*.tmp-*"))
