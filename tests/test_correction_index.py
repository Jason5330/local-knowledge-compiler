from local_kb.cli import build_vault
from local_kb.correction_index import CorrectionIndex
from local_kb.correction_store import CorrectionStore

from test_correction_model import _record


def test_index_rebuilds_from_canonical_records_and_returns_candidates(
    tmp_path,
):
    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    store.create(_record())
    index = CorrectionIndex(paths)

    count = index.rebuild(store)
    ids, truncated = index.candidates(
        space="work",
        terms=("核准", "預算"),
        limit=20,
    )

    assert count == 1
    assert ids == ["COR-20260728-0123456789ab"]
    assert truncated is False
    assert index.integrity_check() is True


def test_index_excludes_inactive_and_cross_space_records(tmp_path):
    from dataclasses import replace

    from local_kb.correction_model import canonical_correction_hash

    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    base = _record()
    for correction_id, status, space in (
        ("COR-20260728-000000000001", "suspended", "work"),
        ("COR-20260728-000000000002", "active", "personal"),
    ):
        changed = replace(
            base,
            correction_id=correction_id,
            status=status,
            applicability=replace(base.applicability, spaces=(space,)),
            content_sha256="",
        )
        store.create(
            replace(
                changed,
                content_sha256=canonical_correction_hash(changed),
            )
        )
    index = CorrectionIndex(paths)
    index.rebuild(store)

    ids, truncated = index.candidates(
        space="work",
        terms=("核准",),
    )

    assert ids == []
    assert truncated is False


def test_index_candidate_limit_reports_truncation(tmp_path):
    from dataclasses import replace

    from local_kb.correction_model import canonical_correction_hash

    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    for number in range(3):
        changed = replace(
            _record(),
            correction_id=f"COR-20260728-{number:012x}",
            content_sha256="",
        )
        store.create(
            replace(
                changed,
                content_sha256=canonical_correction_hash(changed),
            )
        )
    index = CorrectionIndex(paths)
    index.rebuild(store)

    ids, truncated = index.candidates(
        space="work",
        terms=("核准",),
        limit=2,
    )

    assert len(ids) == 2
    assert truncated is True
