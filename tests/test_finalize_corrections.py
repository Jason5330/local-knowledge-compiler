from dataclasses import asdict, replace

import pytest

from local_kb.cli import build_vault
from local_kb.correction_model import canonical_correction_hash
from local_kb.correction_store import CorrectionStore
from local_kb.finalize import finalize_answer
from test_correction_model import _record


def _packet(record=None):
    record = record or _record()
    return {
        "schema_version": 2,
        "question": "核准預算是多少？",
        "evidence": [],
        "applicable_corrections": [{
            "correction_id": record.correction_id,
            "match_level": "strong",
            "content_sha256": record.content_sha256,
            "supporting_evidence": [
                asdict(item) for item in record.supporting_evidence
            ],
        }],
        "possible_corrections": [],
        "correction_scan": {
            "save_allowed": True,
            "index_available": True,
            "truncated": False,
        },
        "correction_warnings": [],
    }


def _answer(decisions):
    return {
        "conclusion": "目前資料無法判定。",
        "citations": [],
        "confidence": "low",
        "conflicts": "證據不足。",
        "correction_decisions": decisions,
    }


def _decision(record=None, decision="applied"):
    record = record or _record()
    return {
        "correction_id": record.correction_id,
        "decision": decision,
        "reason": "本次問題使用相同工作表與萬元單位。",
        "content_sha256": record.content_sha256,
    }


def test_finalize_requires_decision_for_every_applicable_correction(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    CorrectionStore(paths).create(_record())
    with pytest.raises(ValueError, match="missing correction decision"):
        finalize_answer(paths, _packet(), _answer([]))


def test_finalize_rejects_conflict_and_unknown_correction(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    CorrectionStore(paths).create(_record())
    with pytest.raises(ValueError, match="conflict"):
        finalize_answer(paths, _packet(), _answer([_decision(decision="conflict")]))
    unknown = {**_decision(), "correction_id": "COR-20260728-ffffffffffff"}
    with pytest.raises(ValueError, match="unknown correction"):
        finalize_answer(paths, _packet(), _answer([unknown]))


def test_finalize_rejects_packet_when_correction_scan_is_not_saveable(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    packet = _packet()
    packet["correction_scan"]["save_allowed"] = False
    with pytest.raises(ValueError, match="correction scan"):
        finalize_answer(paths, packet, _answer([]))


def test_finalize_reloads_current_record_and_rejects_suspended_rule(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    record = store.create(_record())
    packet = _packet(record)
    suspended = replace(record, status="suspended", content_sha256="")
    suspended = replace(suspended, content_sha256=canonical_correction_hash(suspended))
    store.replace(suspended, expected_hash=record.content_sha256)
    with pytest.raises(ValueError, match="no longer active|changed"):
        finalize_answer(paths, packet, _answer([_decision(record)]))


def test_finalize_saves_applied_correction_audit(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    record = CorrectionStore(paths).create(_record())
    saved = finalize_answer(paths, _packet(record), _answer([_decision(record)]))
    text = saved.read_text(encoding="utf-8")
    assert "## 本次修正紀錄" in text
    assert record.correction_id in text
