from local_kb.cli import build_vault
from local_kb.correction_index import CorrectionIndex
from local_kb.correction_service import CorrectionService
from local_kb.correction_store import CorrectionStore
from local_kb.models import SourceVersion
from test_correction_model import _record


def _source(version_id="ver-2", original_name="budget-report-2026-08.xlsx"):
    return SourceVersion(
        source_id="src-1",
        version_id=version_id,
        space="work",
        original_name=original_name,
        relative_path=f"10_raw/work/src-1/{version_id}/report.xlsx",
        sha256="c" * 64,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        status="extracted",
        previous_version_id="ver-1",
    )


def _service(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    record = store.create(_record())
    CorrectionIndex(paths).rebuild(store)
    return CorrectionService(paths), record


def test_new_matching_source_version_keeps_correction_active_and_records_version(
    tmp_path,
):
    service, record = _service(tmp_path)

    results = service.revalidate_source(
        _source(),
        [("sheet:年度總表;cells:A1-D2", "核准預算\t100\t單位：萬元")],
    )
    updated = service.store.get(record.correction_id)

    assert results[0]["status"] == "active"
    assert "ver-2" in updated.validated_versions


def test_changed_structure_marks_correction_stale(tmp_path):
    service, record = _service(tmp_path)

    service.revalidate_source(
        _source(version_id="ver-3"),
        [("sheet:新版彙總;cells:A1-D2", "已核定\t100\t單位：元")],
    )

    assert service.store.get(record.correction_id).status == "stale"


def test_explicit_exclusion_evidence_suspends_correction(tmp_path):
    service, record = _service(tmp_path)

    service.revalidate_source(
        _source(version_id="ver-4"),
        [(
            "sheet:年度總表;cells:A1-D2",
            "核准預算\t100\t單位明確標示為元",
        )],
    )

    assert service.store.get(record.correction_id).status == "suspended"


def test_user_suspended_correction_is_not_automatically_reactivated(tmp_path):
    service, record = _service(tmp_path)
    suspended = service.transition(
        record.correction_id,
        status="suspended",
        actor="user_via_agent",
        reason="使用者要求暫停",
        expected_hash=record.content_sha256,
    )

    assert service.revalidate_source(
        _source(),
        [("sheet:年度總表;cells:A1-D2", "核准預算\t100\t單位：萬元")],
    ) == []
    assert service.store.get(suspended.correction_id).status == "suspended"
