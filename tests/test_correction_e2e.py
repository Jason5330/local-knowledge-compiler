from local_kb.cli import build_vault
from local_kb.correction_service import CorrectionService
from local_kb.finalize import finalize_answer
from local_kb.query import QueryService
from test_correction_service import _proposal
from test_prepare_corrections import _catalog_with_budget


def test_wrong_answer_becomes_mandatory_correction_for_similar_question(
    tmp_path,
):
    paths = build_vault(tmp_path / "KnowledgeBase")
    catalog = _catalog_with_budget(paths)
    first = QueryService(catalog, vault=paths).prepare(
        "核准預算是多少？",
        {"work"},
    )
    proposal = _proposal(first)
    proposal["applicability"]["source_families"] = ["budget-report"]
    created = CorrectionService(paths).create(first, proposal)

    second = QueryService(catalog, vault=paths).prepare(
        "年度總表核准了多少萬元預算？",
        {"work"},
    )
    matched = second["applicable_corrections"][0]
    evidence = next(
        item
        for item in second["evidence"]
        if item["kind"] == "raw_fragment"
    )
    answer = {
        "conclusion": "核准預算為 100 萬元。",
        "citations": [{
            key: evidence[key]
            for key in (
                "source_id",
                "version_id",
                "locator",
                "evidence_sha256",
            )
        }],
        "confidence": "high",
        "conflicts": "沒有發現衝突。",
        "correction_decisions": [{
            "correction_id": matched["correction_id"],
            "decision": "applied",
            "reason": "相同工作表、欄位與萬元單位。",
            "content_sha256": matched["content_sha256"],
        }],
    }

    saved = finalize_answer(paths, second, answer)

    assert created.record.correction_id in saved.read_text(encoding="utf-8")
