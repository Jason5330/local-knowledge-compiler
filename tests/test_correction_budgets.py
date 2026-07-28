from dataclasses import replace
import json

import pytest

from local_kb.cli import build_vault
from local_kb.correction_index import CorrectionIndex
from local_kb.correction_model import canonical_correction_hash
from local_kb.correction_store import CorrectionStore
from local_kb.query import MAX_PACKET_BYTES, QueryService
from test_correction_model import _record
from test_prepare_corrections import _catalog_with_budget


def _many_records(paths, count=25):
    store = CorrectionStore(paths)
    for number in range(count):
        base = _record()
        changed = replace(
            base,
            correction_id=f"COR-20260728-{number:012x}",
            content_sha256="",
        )
        changed = replace(
            changed,
            content_sha256=canonical_correction_hash(changed),
        )
        store.create(changed)
    CorrectionIndex(paths).rebuild(store)
    return store


def test_match_and_packet_budgets_fail_closed(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    _many_records(paths)

    packet = QueryService(
        _catalog_with_budget(paths),
        vault=paths,
    ).prepare("年度總表核准預算是多少萬元？", {"work"})

    assert len(packet["applicable_corrections"]) <= 20
    assert len(packet["possible_corrections"]) <= 10
    assert packet["correction_scan"]["truncated"] is True
    assert packet["correction_scan"]["save_allowed"] is False
    assert len(json.dumps(
        packet,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")) <= MAX_PACKET_BYTES


def test_index_rejects_too_many_terms_or_candidates(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    index = CorrectionIndex(paths)
    index.initialize()

    with pytest.raises(ValueError, match="invalid"):
        index.candidates(
            space="work",
            terms=tuple(str(number) for number in range(65)),
        )
    with pytest.raises(ValueError, match="invalid"):
        index.candidates(
            space="work",
            terms=(),
            limit=201,
        )


def test_oversized_record_is_rejected_before_json_decode(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    correction_id = "COR-20260728-ffffffffffff"
    path = paths.correction_records / f"{correction_id}.json"
    path.write_bytes(b"{" + b"x" * store.MAX_RECORD_BYTES + b"}")

    with pytest.raises(ValueError, match="size"):
        store.get(correction_id)


def test_full_timeline_rejects_next_event_without_changing_bytes(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    record = store.create(_record())
    path = paths.correction_timeline / f"{record.correction_id}.jsonl"
    path.write_bytes(b"x" * store.MAX_TIMELINE_BYTES)
    before = path.read_bytes()

    with pytest.raises(ValueError, match="timeline.*size"):
        store.append_event(
            record.correction_id,
            event_type="occurrence",
            actor="codex",
            reason="bounded timeline test",
            details={},
        )

    assert path.read_bytes() == before
