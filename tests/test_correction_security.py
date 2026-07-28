from dataclasses import replace
import json

import pytest

from local_kb.cli import build_vault
from local_kb.correction_index import CorrectionIndex
from local_kb.correction_model import canonical_correction_hash
from local_kb.correction_service import CorrectionService
from local_kb.correction_store import CorrectionStore
from local_kb.finalize import finalize_answer
from local_kb.query import QueryService
from test_correction_model import _record
from test_correction_service import _proposal
from test_correction_revalidation import _source
from test_prepare_corrections import _catalog_with_budget


def test_personal_correction_never_appears_in_work_packet(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    base = _record()
    personal = replace(
        base,
        applicability=replace(
            base.applicability,
            spaces=("personal",),
        ),
        content_sha256="",
    )
    personal = replace(
        personal,
        content_sha256=canonical_correction_hash(personal),
    )
    store = CorrectionStore(paths)
    store.create(personal)
    CorrectionIndex(paths).rebuild(store)

    packet = QueryService(
        _catalog_with_budget(paths),
        vault=paths,
    ).prepare("核准預算是多少？", {"work"})

    assert packet["applicable_corrections"] == []
    assert packet["possible_corrections"] == []


def test_tampered_correction_blocks_finalize(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    catalog = _catalog_with_budget(paths)
    first = QueryService(catalog, vault=paths).prepare(
        "核准預算是多少？",
        {"work"},
    )
    proposal = _proposal(first)
    proposal["applicability"]["source_families"] = ["budget-report"]
    record = CorrectionService(paths).create(first, proposal).record
    packet = QueryService(catalog, vault=paths).prepare(
        "年度總表核准了多少萬元預算？",
        {"work"},
    )
    path = paths.correction_records / f"{record.correction_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["correction_rule"] = "被竄改但沒有重算雜湊。"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    matched = packet["applicable_corrections"][0]
    answer = {
        "conclusion": "無法安全保存。",
        "citations": [],
        "confidence": "low",
        "conflicts": "修正遭修改。",
        "correction_decisions": [{
            "correction_id": matched["correction_id"],
            "decision": "applied",
            "reason": "測試遭竄改的規則。",
            "content_sha256": matched["content_sha256"],
        }],
    }

    with pytest.raises(ValueError, match="hash|changed"):
        finalize_answer(paths, packet, answer)


def test_derived_or_invented_evidence_cannot_create_correction(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    catalog = _catalog_with_budget(paths)
    packet = QueryService(catalog, vault=paths).prepare(
        "核准預算是多少？",
        {"work"},
    )
    proposal = _proposal(packet)
    proposal["supporting_evidence"][0]["source_id"] = "derived-wiki"

    with pytest.raises(ValueError, match="supporting evidence"):
        CorrectionService(paths).create(packet, proposal)


def test_retired_correction_remains_retired_after_revalidation(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    record = store.create(_record())
    CorrectionIndex(paths).rebuild(store)
    service = CorrectionService(paths)
    retired = service.transition(
        record.correction_id,
        status="retired",
        actor="user_via_agent",
        reason="永久退役測試",
        expected_hash=record.content_sha256,
    )

    assert service.revalidate_source(
        _source(),
        [("sheet:年度總表;cells:A1-D2", "核准預算 100 單位 萬元")],
    ) == []
    assert service.store.get(retired.correction_id).status == "retired"
