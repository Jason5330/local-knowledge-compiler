import json
import os
from pathlib import Path

import pytest


def _source(*, source_id: str, version_id: str, space: str, name: str, digest: str):
    from local_kb.models import SourceVersion

    return SourceVersion(
        source_id=source_id,
        version_id=version_id,
        space=space,
        original_name=name,
        relative_path=f"10_raw/{space}/{source_id}/{version_id}/{name}",
        sha256=digest * 64,
        media_type="text/plain",
        status="published",
    )


@pytest.fixture
def seeded_catalog(tmp_path):
    from local_kb.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        _source(source_id="src-work", version_id="ver-work", space="work", name="roadmap.txt", digest="a"),
        [("line:1", "Project Aurora launch checklist and owner list")],
    )
    catalog.upsert_source(
        _source(source_id="src-personal", version_id="ver-personal", space="personal", name="private.txt", digest="b"),
        [("line:1", "Project Aurora private medical appointment")],
    )
    catalog.upsert_source(
        _source(source_id="src-cjk", version_id="ver-cjk", space="work", name="chinese.txt", digest="c"),
        [("line:7", "知識庫要保留原始證據與定位資訊")],
    )
    return catalog


def test_prepare_isolates_space_and_keeps_raw_provenance(seeded_catalog):
    from local_kb.query import QueryService

    packet = QueryService(seeded_catalog).prepare("Project Aurora", {"work"})

    assert packet["status"] == "ready"
    assert packet["spaces"] == ["work"]
    assert packet["evidence"]
    assert packet["truncated"] == {
        "evidence": False, "pending_jobs": False,
        "result_limit_reached": False, "wiki_scan": False,
    }
    assert all(hit["space"] == "work" for hit in packet["evidence"])
    assert packet["evidence"][0]["kind"] == "raw_fragment"
    assert {"source_id", "version_id", "path", "locator", "score", "text"} <= set(packet["evidence"][0])


def test_prepare_handles_cjk_and_honestly_reports_no_evidence(seeded_catalog):
    from local_kb.query import QueryService

    assert QueryService(seeded_catalog).prepare("原始證據", {"work"})["status"] == "ready"
    packet = QueryService(seeded_catalog).prepare("!!!", {"work"})
    assert packet["status"] == "insufficient_evidence"
    assert packet["evidence"] == []
    assert packet["reason"] == "question_has_no_searchable_terms"


def test_prepare_fallback_retrieves_natural_cjk_and_english_questions(seeded_catalog):
    from local_kb.query import QueryService

    service = QueryService(seeded_catalog)
    assert service.prepare("請問知識庫如何保留原始證據？", {"work"})["status"] == "ready"
    assert service.prepare("what is the Project Aurora?", {"work"})["status"] == "ready"
    assert service.prepare("請問量子香蕉如何跳舞？", {"work"})["status"] == "insufficient_evidence"


def test_prepare_stably_deduplicates_results_and_rejects_unsafe_inputs(seeded_catalog):
    from local_kb.query import QueryService

    service = QueryService(seeded_catalog)
    first = service.prepare("Project Project Aurora", {"work"})
    second = service.prepare("Project Project Aurora", {"work"})
    assert first["evidence"] == second["evidence"]
    assert len({(hit["kind"], hit["version_id"], hit["locator"]) for hit in first["evidence"]}) == len(first["evidence"])
    with pytest.raises(ValueError):
        service.prepare("ok\x00", {"work"})
    with pytest.raises(ValueError):
        service.prepare("ok", {"project:BAD"})
    with pytest.raises(TypeError):
        service.prepare("ok", "work")
    assert service.prepare("Aurora", (space for space in ("work", "work")))["spaces"] == ["work"]
    with pytest.raises(TypeError):
        service.prepare("ok", (["work"],))
    with pytest.raises(ValueError):
        service.prepare("ok", tuple(f"project:p-{number}" for number in range(17)))
    with pytest.raises(ValueError):
        service.prepare("!", {"work"}, limit=0)
    assert service.prepare("   ", {"work"})["status"] == "insufficient_evidence"


def test_prepare_includes_bounded_pending_job_metadata(seeded_catalog, tmp_path):
    from local_kb.query import QueryService
    from local_kb.queue import DiskQueue

    queue = DiskQueue(tmp_path / "queue")
    job = queue.enqueue(tmp_path / "missing.txt", job_id="pending-job")
    def mark_pending(current):
        current.metadata.update({"space": "work", "source_id": "src-pending", "compiler_status": "needs_agent"})
        current.state = "pending_attention"
        current.error = "x" * 500
    queue.update(job.job_id, mark_pending)

    packet = QueryService(seeded_catalog, queue=queue).prepare("Aurora", {"work"})

    assert packet["pending_jobs"]["scope"] == "selected_spaces"
    assert packet["pending_jobs"]["jobs"] == [{
        "job_id": "pending-job", "state": "pending_attention", "compiler_status": "needs_agent", "source_status": None, "pending_reason": "needs_agent", "relation": "unknown",
        "source": "src-pending", "space": "work", "error": "x" * 256,
    }]


def test_prepare_keeps_published_pending_extractor_and_unknown_space_visible(seeded_catalog, tmp_path):
    from local_kb.query import QueryService
    from local_kb.queue import DiskQueue

    queue = DiskQueue(tmp_path / "queue")
    job = queue.enqueue(tmp_path / "deleted.png", job_id="ocr-job")
    def mark_published_pending(current):
        current.state = "published"
        current.metadata["source"] = {"source_id": "src-image", "original_name": "scan.png", "status": "pending_extractor"}
    queue.update(job.job_id, mark_published_pending)

    packet = QueryService(seeded_catalog, queue=queue).prepare("Aurora", {"work"})
    pending = packet["pending_jobs"]
    assert pending["scope"] == "selected_spaces_plus_unknown"
    assert pending["jobs"] == [{
        "job_id": "ocr-job", "state": "published", "compiler_status": None, "source_status": "pending_extractor", "pending_reason": "extractor_required", "relation": "unknown",
        "source": "src-image", "space": None, "error": None,
    }]


def test_prepare_packet_json_is_stable_and_cli_writes_inside_vault(tmp_path, capsys):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault, main

    vault = build_vault(tmp_path / "vault")
    catalog = Catalog(vault.index / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        _source(source_id="src-cli", version_id="ver-cli", space="work", name="cli.txt", digest="d"),
        [("line:1", "CLI proof evidence")],
    )
    output = vault.runtime / "packet.json"
    assert main(["prepare", "CLI proof", "--vault", str(vault.root), "--space", "work", "--output", ".kb/packet.json"]) == 0
    assert Path(capsys.readouterr().out.strip()) == output.resolve()
    packet = json.loads(output.read_text(encoding="utf-8"))
    assert packet["schema_version"] == 1
    assert packet["status"] == "ready"
    assert main(["prepare", "CLI proof", "--vault", str(vault.root), "--space", "work", "--output", ".kb/packet.json"]) == 0
    assert main(["prepare", "CLI proof", "--vault", str(vault.root), "--space", "work", "--output", "../outside.json"]) == 1
    assert not (tmp_path / "outside.json").exists()
    assert main(["prepare", "CLI proof", "--vault", str(vault.root), "--space", "work", "--output", ".kb/CON.json"]) == 1


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="symlink support unavailable")
def test_prepare_cli_refuses_symlinked_output_parent(tmp_path):
    from local_kb.cli import build_vault, main

    vault = build_vault(tmp_path / "vault")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = vault.runtime / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privileges unavailable")
    assert main(["prepare", "nothing", "--vault", str(vault.root), "--output", ".kb/link/packet.json"]) == 1
    assert not (outside / "packet.json").exists()


def test_prepare_wiki_title_route_is_secondary_and_bounded(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.query import QueryService

    vault = build_vault(tmp_path / "vault")
    (vault.wiki / "work" / "aurora.md").parent.mkdir(exist_ok=True)
    (vault.wiki / "work" / "aurora.md").write_text(
        "---\ntitle: \"Aurora plan\"\naliases:\n  - \"launch plan\"\ntype: \"topic\"\nspace: \"work\"\nsource_ids:\n  - \"src-raw\"\n---\n\n## Current State\n\nDerived summary only.\n\n## Evidence\n\n- src-raw\n",
        encoding="utf-8",
    )
    catalog = Catalog(vault.index / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(
        _source(source_id="src-raw", version_id="ver-raw", space="work", name="a.txt", digest="e"),
        [("line:3", "Aurora raw primary evidence")],
    )
    raw_file = vault.root / "10_raw/work/src-raw/ver-raw/a.txt"
    raw_file.parent.mkdir(parents=True); raw_file.write_text("backing", encoding="utf-8")
    packet = QueryService(catalog, vault=vault).prepare("Aurora", {"work"})
    assert packet["evidence"][0]["kind"] == "raw_fragment"
    derived = [item for item in packet["evidence"] if item["kind"] == "derived_wiki"]
    assert derived == [{
        "kind": "derived_wiki", "evidence_class": "derived", "space": "work",
        "path": "20_wiki/work/aurora.md", "locator": "Current State",
        "text": "Derived summary only.", "source_ids": ["src-raw"], "score": 2.0,
        "route": "direct_wiki", "routes": ["Aurora"], "coverage": 1, "match_kind": "direct",
        "truncated": False,
    }]


def test_prepare_derived_only_result_is_ready_without_raw_no_match_reason(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.query import QueryService

    vault = build_vault(tmp_path / "vault")
    (vault.wiki / "work" / "only.md").write_text(
        "---\ntitle: \"derived route\"\nspace: \"work\"\nsource_ids:\n  - \"src-derived\"\n---\n\n## Current State\n\nderived only text\n",
        encoding="utf-8",
    )
    catalog = Catalog(vault.index / "catalog.sqlite3")
    catalog.initialize()
    catalog.upsert_source(_source(source_id="src-derived", version_id="ver-derived", space="work", name="only.txt", digest="a"), [("line:1", "derived backing")])
    raw_file = vault.root / "10_raw/work/src-derived/ver-derived/only.txt"
    raw_file.parent.mkdir(parents=True); raw_file.write_text("backing", encoding="utf-8")
    packet = QueryService(catalog, vault=vault).prepare("derived route", {"work"})
    assert packet["status"] == "ready"
    assert "reason" not in packet


def test_prepare_rejects_wiki_page_with_fake_provenance(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.query import QueryService

    vault = build_vault(tmp_path / "vault")
    (vault.wiki / "work" / "fake.md").write_text("---\ntitle: \"fake route\"\nspace: \"work\"\nsource_ids:\n  - \"fake-source\"\n---\n\n## Current State\n\nfake text\n", encoding="utf-8")
    catalog = Catalog(vault.index / "catalog.sqlite3"); catalog.initialize()
    packet = QueryService(catalog, vault=vault).prepare("fake route", {"work"})
    assert packet["status"] == "insufficient_evidence"
    assert packet["warnings"] == ["wiki_page_skipped_invalid_provenance"]


def test_exact_identifier_route_inside_question_is_safe_and_stable(seeded_catalog):
    from local_kb.query import QueryService

    packet = QueryService(seeded_catalog).prepare("請查看 ver-work 的資料", {"work"})
    assert packet["evidence"][0]["version_id"] == "ver-work"
    assert packet["evidence"][0]["score"] >= 1


def test_prepare_wiki_walk_is_bounded_by_depth_and_entries(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.query import QueryService

    vault = build_vault(tmp_path / "vault")
    deep = vault.wiki / "work"
    for number in range(10):
        deep = deep / f"level{number}"
    deep.mkdir(parents=True)
    (deep / "hidden.md").write_text(
        "---\ntitle: \"hidden route\"\nspace: \"work\"\n---\n\n## Current State\n\nnot scanned\n",
        encoding="utf-8",
    )
    for number in range(400):
        (vault.wiki / "work" / f"a{number:03}.md").write_text("not wiki", encoding="utf-8")
    (vault.wiki / "work" / "zzz.md").write_text(
        "---\ntitle: \"overflow route\"\nspace: \"work\"\n---\n\n## Current State\n\nnot scanned\n",
        encoding="utf-8",
    )
    catalog = Catalog(vault.index / "catalog.sqlite3")
    catalog.initialize()
    packet = QueryService(catalog, vault=vault).prepare("overflow route", {"work"})
    assert packet["status"] == "insufficient_evidence"


def test_pending_jobs_report_total_shown_relation_and_truncation(seeded_catalog, tmp_path):
    from local_kb.query import QueryService
    from local_kb.queue import DiskQueue

    queue = DiskQueue(tmp_path / "queue")
    for number in range(41):
        job = queue.enqueue(tmp_path / f"aurora-{number}.txt", job_id=f"job-{number}")
        queue.update(job.job_id, lambda current: current.metadata.update({"space": "work", "source_id": f"aurora-{number}"}))
    pending = QueryService(seeded_catalog, queue=queue).prepare("Aurora", {"work"})["pending_jobs"]
    assert (pending["total"], pending["shown"], pending["truncated"]) == (41, 40, True)
    assert pending["jobs"][0]["relation"] == "matched_metadata"


def test_prepare_keeps_derived_wiki_when_raw_hits_fill_limit(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.query import QueryService

    vault = build_vault(tmp_path / "vault")
    catalog = Catalog(vault.index / "catalog.sqlite3"); catalog.initialize()
    raw = vault.raw / "work" / "src-wiki" / "ver-wiki" / "source.txt"
    raw.parent.mkdir(parents=True); raw.write_text("wiki source", encoding="utf-8")
    catalog.upsert_source(_source(source_id="src-wiki", version_id="ver-wiki", space="work", name="source.txt", digest="f"), [("line:1", "Aurora wiki backing source")])
    for number in range(12):
        catalog.upsert_source(_source(source_id=f"src-{number}", version_id=f"ver-{number}", space="work", name=f"{number}.txt", digest=f"{number:x}"), [("line:1", "Aurora raw evidence")])
    (vault.wiki / "work" / "page.md").write_text("---\ntitle: \"Aurora\"\nspace: \"work\"\nsource_ids:\n  - \"src-wiki\"\n---\n\n## Current State\n\nAurora wiki summary\n", encoding="utf-8")
    evidence = QueryService(catalog, vault=vault).prepare("Aurora", {"work"}, limit=12)["evidence"]
    assert evidence[0]["kind"] == "raw_fragment"
    assert any(item["kind"] == "derived_wiki" for item in evidence)


@pytest.mark.skipif(os.name == "nt", reason="POSIX fd-relative open race test")
def test_prepare_wiki_parent_replacement_cannot_redirect_pinned_read(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    import local_kb.query as query
    from local_kb.query import QueryService

    vault = build_vault(tmp_path / "vault")
    work = vault.wiki / "work"
    original = work / "page.md"
    original.write_text("---\ntitle: \"route\"\nspace: \"work\"\n---\n\n## Current State\n\noriginal safe text\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "page.md").write_text("---\ntitle: \"route\"\nspace: \"work\"\n---\n\n## Current State\n\noutside text\n", encoding="utf-8")
    real_open = query._open_pinned_regular
    replaced = False

    @contextmanager
    def replace_parent_then_open(path, **kwargs):
        nonlocal replaced
        if not replaced:
            replaced = True
            parked = vault.wiki / "work-original"
            os.rename(work, parked)
            work.symlink_to(outside, target_is_directory=True)
        with real_open(path, **kwargs) as opened:
            yield opened

    monkeypatch.setattr(query, "_open_pinned_regular", replace_parent_then_open)
    catalog = Catalog(vault.index / "catalog.sqlite3")
    catalog.initialize()
    packet = QueryService(catalog, vault=vault).prepare("route", {"work"})
    assert packet["evidence"][0]["text"] == "original safe text"
