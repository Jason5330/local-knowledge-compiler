from local_kb.catalog import Catalog
from local_kb.cli import build_vault
from local_kb.correction_index import CorrectionIndex
from local_kb.correction_store import CorrectionStore
from local_kb.models import SourceVersion
from local_kb.query import QueryService

from test_correction_model import _record


def _catalog_with_budget(paths):
    catalog = Catalog(paths.index / "catalog.sqlite3")
    catalog.initialize()
    source = SourceVersion(
        source_id="src-1",
        version_id="ver-1",
        space="work",
        original_name="budget-report.xlsx",
        relative_path="10_raw/work/src-1/ver-1/budget-report.xlsx",
        sha256="c" * 64,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        status="extracted",
    )
    catalog.upsert_source(
        source,
        [
            (
                "sheet:年度總表;cells:A1-D2",
                "核准預算 100 單位 萬元",
            )
        ],
    )
    return catalog


def test_prepare_injects_applicable_corrections_with_scan_metadata(
    tmp_path,
):
    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    store.create(_record())
    CorrectionIndex(paths).rebuild(store)
    catalog = _catalog_with_budget(paths)

    packet = QueryService(catalog, vault=paths).prepare(
        "年度總表的核准預算是多少萬元？",
        {"work"},
    )

    assert packet["schema_version"] == 2
    assert (
        packet["applicable_corrections"][0]["correction_id"]
        == "COR-20260728-0123456789ab"
    )
    assert packet["correction_scan"]["index_available"] is True
    assert packet["correction_scan"]["save_allowed"] is True
    assert packet["instructions"][-1].startswith(
        "逐項處理 applicable_corrections"
    )


def test_prepare_fails_closed_when_correction_index_is_corrupt(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    paths.correction_index.write_bytes(b"not sqlite")
    catalog = _catalog_with_budget(paths)

    packet = QueryService(catalog, vault=paths).prepare(
        "核准預算是多少？",
        {"work"},
    )

    assert packet["correction_scan"]["index_available"] is False
    assert packet["correction_scan"]["save_allowed"] is False
    assert "correction_unavailable" in packet["correction_warnings"]


def test_prepare_empty_correction_store_needs_no_index(tmp_path):
    paths = build_vault(tmp_path / "KnowledgeBase")
    catalog = _catalog_with_budget(paths)

    packet = QueryService(catalog, vault=paths).prepare(
        "核准預算是多少？",
        {"work"},
    )

    assert packet["applicable_corrections"] == []
    assert packet["possible_corrections"] == []
    assert packet["correction_scan"]["save_allowed"] is True
    assert not paths.correction_index.exists()


def test_prepare_blocks_saving_when_candidate_scan_is_truncated(
    tmp_path,
    monkeypatch,
):
    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    store.create(_record())
    CorrectionIndex(paths).rebuild(store)
    catalog = _catalog_with_budget(paths)
    monkeypatch.setattr(
        CorrectionIndex,
        "candidates",
        lambda *args, **kwargs: (
            ["COR-20260728-0123456789ab"],
            True,
        ),
    )

    packet = QueryService(catalog, vault=paths).prepare(
        "核准預算是多少？",
        {"work"},
    )

    assert packet["correction_scan"]["truncated"] is True
    assert packet["correction_scan"]["save_allowed"] is False
