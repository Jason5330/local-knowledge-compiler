import pytest

from local_kb.cli import build_vault
from local_kb.correction_service import CorrectionService


def _packet():
    evidence = {
        "kind": "raw_fragment",
        "source_id": "src-1",
        "version_id": "ver-1",
        "space": "work",
        "path": "10_raw/work/src-1/ver-1/report.xlsx",
        "locator": "sheet:年度總表;cells:A1-D2",
        "text": "核准預算\t100\t單位：萬元",
        "evidence_sha256": "replace-in-test",
    }
    from local_kb.query import evidence_sha256

    evidence["evidence_sha256"] = evidence_sha256(evidence)
    return {
        "schema_version": 2,
        "question": "核准預算是多少？",
        "spaces": ["work"],
        "evidence": [evidence],
    }


def _proposal(packet):
    evidence = packet["evidence"][0]
    return {
        "trigger_type": "user_reported_wrong",
        "created_by": "codex",
        "wrong_answer_summary": "把萬元當成元。",
        "error_type": "unit_error",
        "correction_rule": "核准預算欄位以萬元表示，不得當成元。",
        "applicability": {
            "spaces": ["work"],
            "file_types": ["xlsx"],
            "source_families": ["report"],
            "sheet_names": ["年度總表"],
            "column_names": ["核准預算"],
            "units": ["萬元"],
            "question_types": ["amount_lookup"],
            "keywords": ["核准", "預算"],
            "error_types": ["unit_error"],
        },
        "exclusions": ["本次工作表明確標示單位為元時不適用。"],
        "supporting_evidence": [
            {
                key: evidence[key]
                for key in (
                    "source_id",
                    "version_id",
                    "locator",
                    "evidence_sha256",
                )
            }
        ],
        "user_report": "這個回答錯了。",
    }


def test_user_report_creates_immediately_active_grounded_correction(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    service = CorrectionService(paths)
    packet = _packet()

    result = service.create(packet, _proposal(packet))

    assert result.record.status == "active"
    assert result.record.trigger_type == "user_reported_wrong"
    assert result.created is True
    assert service.store.get(result.record.correction_id) == result.record


def test_creation_rejects_unknown_or_derived_supporting_evidence(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    service = CorrectionService(paths)
    packet = _packet()
    proposal = _proposal(packet)
    proposal["supporting_evidence"][0]["version_id"] = "invented"

    with pytest.raises(ValueError, match="supporting evidence"):
        service.create(packet, proposal)


def test_subjective_hunch_is_not_a_valid_trigger(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    service = CorrectionService(paths)
    packet = _packet()
    proposal = _proposal(packet)
    proposal.pop("user_report")
    proposal["trigger_type"] = "deterministic_validation_failure"
    proposal["validation"] = {"kind": "subjective_hunch"}

    with pytest.raises(ValueError, match="deterministic"):
        service.create(packet, proposal)


def test_duplicate_correction_appends_occurrence_instead_of_new_record(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    service = CorrectionService(paths)
    packet = _packet()

    first = service.create(packet, _proposal(packet))
    second = service.create(packet, _proposal(packet))

    records, truncated = service.store.iter_records()
    assert truncated is False
    assert len(records) == 1
    assert second.created is False
    assert second.record.correction_id == first.record.correction_id
    assert [event["event_type"] for event in service.store.events(
        first.record.correction_id
    )] == ["created", "occurrence"]
