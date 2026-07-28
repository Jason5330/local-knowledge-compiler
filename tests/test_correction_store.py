from dataclasses import replace

import pytest

from local_kb.cli import build_vault
from local_kb.correction_model import canonical_correction_hash
from local_kb.correction_store import CorrectionStore

from test_correction_model import _record


def _with_id(record, correction_id):
    changed = replace(
        record,
        correction_id=correction_id,
        content_sha256="",
    )
    return replace(
        changed,
        content_sha256=canonical_correction_hash(changed),
    )


def test_store_publishes_one_record_and_appends_timeline(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    record = store.create(_record())
    event = store.append_event(
        record.correction_id,
        event_type="created",
        actor="codex",
        reason="user_reported_wrong",
        details={"question": record.original_question},
    )

    assert store.get(record.correction_id) == record
    assert store.events(record.correction_id) == [event]
    assert event["event_type"] == "created"


def test_store_never_replaces_existing_record_during_create(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    record = store.create(_record())

    with pytest.raises(FileExistsError):
        store.create(record)


def test_store_replace_uses_expected_hash_and_preserves_last_good_record(
    tmp_path,
):
    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    record = store.create(_record())
    changed = replace(
        record,
        status="suspended",
        content_sha256="",
    )
    changed = replace(
        changed,
        content_sha256=canonical_correction_hash(changed),
    )

    with pytest.raises(ValueError, match="changed"):
        store.replace(changed, expected_hash="f" * 64)
    assert store.get(record.correction_id) == record

    assert store.replace(
        changed,
        expected_hash=record.content_sha256,
    ) == changed


def test_store_rejects_symlinked_record_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "KnowledgeBase"
    root.mkdir()
    corrections = root / "50_corrections"
    try:
        corrections.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")

    with pytest.raises(ValueError, match="unsafe|link|reparse"):
        CorrectionStore(root)


def test_bounded_scan_reports_truncation(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    for index in range(3):
        store.create(
            _with_id(_record(), f"COR-20260728-{index:012x}")
        )

    records, truncated = store.iter_records(
        max_records=2,
        max_bytes=1_000_000,
    )

    assert len(records) == 2
    assert truncated is True


def test_store_rejects_corrupt_or_oversized_record(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    path = paths.correction_records / "COR-20260728-0123456789ab.json"
    path.write_text("{bad json", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON|record"):
        store.get("COR-20260728-0123456789ab")

    path.write_bytes(b"x" * (CorrectionStore.MAX_RECORD_BYTES + 1))
    with pytest.raises(ValueError, match="size"):
        store.get("COR-20260728-0123456789ab")
