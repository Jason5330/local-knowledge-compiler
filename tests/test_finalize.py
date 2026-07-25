import json
import os
from pathlib import Path

import pytest


def _packet() -> dict:
    from local_kb.query import evidence_sha256

    packet = {
        "schema_version": 1,
        "question": "應該採用哪一個方案？",
        "evidence": [
            {
                "kind": "raw_fragment",
                "source_id": "src-ok",
                "version_id": "ver-1",
                "locator": "line:7",
                "path": "10_raw/work/src-ok/ver-1/note.md",
                "space": "work",
                "text": "採用 B 方案。",
            },
            {
                "kind": "derived_wiki",
                "path": "20_wiki/work/decisions/choice.md",
                "locator": "Current State",
                "source_ids": ["src-wiki"],
                "space": "work",
                "text": "目前決策是 B。",
            },
        ],
    }
    for item in packet["evidence"]:
        item["evidence_sha256"] = evidence_sha256(item)
    return packet


def _answer(citations=None) -> dict:
    packet = _packet()
    return {
        "conclusion": "採用 B 方案。",
        "citations": (
            [{
                "source_id": "src-ok",
                "version_id": "ver-1",
                "locator": "line:7",
                "evidence_sha256": packet["evidence"][0]["evidence_sha256"],
            }]
            if citations is None
            else citations
        ),
        "confidence": "medium",
        "conflicts": "無",
    }


def test_finalize_rejects_unknown_source(tmp_path):
    from local_kb.finalize import finalize_answer

    packet = {"question": "Q", "evidence": [{"source_id": "src_ok"}]}
    answer = {"conclusion": "A", "citations": ["src_missing"], "confidence": "high"}

    with pytest.raises(ValueError, match="unknown citation"):
        finalize_answer(tmp_path, packet, answer)


def test_finalize_saves_answer_with_exact_provenance(tmp_path):
    from local_kb.finalize import finalize_answer

    path = finalize_answer(tmp_path, _packet(), _answer())
    text = path.read_text(encoding="utf-8")

    assert path.is_relative_to(tmp_path / "30_answers")
    assert "type: derived-answer" in text
    assert "label: 衍生知識" in text
    assert "src-ok" in text
    assert "ver-1" in text
    assert "line:7" in text
    assert "採用 B 方案。" in text


@pytest.mark.parametrize(
    "citation",
    [
        {"source_id": "src-ok", "version_id": "wrong", "locator": "line:7"},
        {"source_id": "src-ok", "version_id": "ver-1", "locator": "wrong"},
        {
            "path": "20_wiki/work/decisions/wrong.md",
            "locator": "Current State",
            "source_ids": ["src-wiki"],
        },
    ],
)
def test_finalize_requires_an_exact_packet_evidence_identity(tmp_path, citation):
    from local_kb.finalize import finalize_answer

    packet = _packet()
    if "source_id" in citation:
        citation["evidence_sha256"] = packet["evidence"][0]["evidence_sha256"]
    else:
        citation["evidence_sha256"] = packet["evidence"][1]["evidence_sha256"]
    with pytest.raises(ValueError, match="unknown citation"):
        finalize_answer(tmp_path, packet, _answer([citation]))


@pytest.mark.parametrize(
    "citations",
    [
        "src-ok",
        [1],
        [{"source_id": "src-ok", "version_id": "ver-1"}],
        [
            {"source_id": "src-ok", "version_id": "ver-1", "locator": "line:7"},
            {"source_id": "src-ok", "version_id": "ver-1", "locator": "line:7"},
        ],
    ],
)
def test_finalize_rejects_wrong_or_duplicate_citations(tmp_path, citations):
    from local_kb.finalize import finalize_answer

    with pytest.raises((TypeError, ValueError), match="citation"):
        finalize_answer(tmp_path, _packet(), _answer(citations))


def test_finalize_allows_honest_uncited_answer(tmp_path):
    from local_kb.finalize import finalize_answer

    path = finalize_answer(
        tmp_path,
        {"question": "未知問題", "evidence": []},
        {"conclusion": "目前無法確定", "citations": [], "confidence": "low"},
    )

    assert "- 無可用來源" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("packet", "answer"),
    [
        ({"question": 7, "evidence": []}, _answer([])),
        ({"question": "Q\x00", "evidence": []}, _answer([])),
        ({"question": "Q", "evidence": "bad"}, _answer([])),
        ({"question": "Q", "evidence": [{"source_id": "../bad"}]}, {"conclusion": "A", "citations": ["../bad"]}),
        (_packet(), {"conclusion": ["not text"], "citations": []}),
        (_packet(), {"conclusion": "A", "citations": [], "confidence": "certain"}),
    ],
)
def test_finalize_rejects_malformed_or_unsafe_documents(tmp_path, packet, answer):
    from local_kb.finalize import finalize_answer

    with pytest.raises((TypeError, ValueError)):
        finalize_answer(tmp_path, packet, answer)


def test_finalize_escapes_markdown_and_yaml_control_content(tmp_path):
    from local_kb.finalize import finalize_answer

    packet = {"question": "---\ntitle: forged", "evidence": []}
    answer = {
        "conclusion": "<script>alert(1)</script>\n---",
        "citations": [],
        "confidence": "low",
        "conflicts": "none",
    }
    path = finalize_answer(tmp_path, packet, answer)
    text = path.read_text(encoding="utf-8")

    assert 'question: "---\\ntitle: forged"' in text
    assert text.count("\n---\n") == 1


def test_finalize_rejects_multiline_locator_even_when_packet_matches(tmp_path):
    from local_kb.finalize import finalize_answer

    packet = _packet()
    packet["evidence"][0]["locator"] = "line:7\n- forged"
    answer = _answer([
        {
            "source_id": "src-ok",
            "version_id": "ver-1",
            "locator": "line:7\n- forged",
        }
    ])

    with pytest.raises(ValueError, match="locator"):
        finalize_answer(tmp_path, packet, answer)


def test_finalize_citation_block_cannot_be_closed_by_backticks_or_raw_html(tmp_path):
    from local_kb.finalize import finalize_answer
    from local_kb.query import evidence_sha256

    packet = _packet()
    locator = "section ````` <img src=x onerror=alert(1)>"
    packet["evidence"][0]["locator"] = locator
    packet["evidence"][0]["evidence_sha256"] = evidence_sha256(packet["evidence"][0])
    answer = _answer([{
        "source_id": "src-ok",
        "version_id": "ver-1",
        "locator": locator,
        "evidence_sha256": packet["evidence"][0]["evidence_sha256"],
    }])
    text = finalize_answer(tmp_path, packet, answer).read_text(encoding="utf-8")

    assert "<img" not in text
    assert "&lt;img" in text
    assert "\n``````json\n" in text
    assert "\n``````\n" in text


def test_finalize_requires_packet_generated_wiki_digest_and_persists_it(tmp_path):
    from local_kb.finalize import finalize_answer
    from local_kb.query import evidence_sha256

    packet = _packet()
    wiki = packet["evidence"][1]
    wiki["evidence_sha256"] = evidence_sha256(wiki)
    citation = {
        "path": wiki["path"],
        "locator": wiki["locator"],
        "source_ids": wiki["source_ids"],
        "evidence_sha256": wiki["evidence_sha256"],
    }
    path = finalize_answer(tmp_path, packet, _answer([citation]))

    assert wiki["evidence_sha256"] in path.read_text(encoding="utf-8")
    forged = dict(packet)
    forged["evidence"] = [dict(wiki, evidence_sha256="0" * 64)]
    with pytest.raises(ValueError, match="evidence_sha256"):
        finalize_answer(tmp_path, forged, _answer([dict(citation, evidence_sha256="0" * 64)]))


def test_finalize_keeps_packet_wiki_digest_after_live_wiki_changes(tmp_path):
    from local_kb.finalize import finalize_answer
    from local_kb.query import evidence_sha256

    packet = _packet()
    wiki = packet["evidence"][1]
    wiki["evidence_sha256"] = evidence_sha256(wiki)
    live = tmp_path / wiki["path"]
    live.parent.mkdir(parents=True)
    live.write_text("changed after prepare", encoding="utf-8")
    citation = {
        "path": wiki["path"],
        "locator": wiki["locator"],
        "source_ids": wiki["source_ids"],
        "evidence_sha256": wiki["evidence_sha256"],
    }

    text = finalize_answer(tmp_path, packet, _answer([citation])).read_text(encoding="utf-8")
    assert wiki["evidence_sha256"] in text
    assert "changed after prepare" not in text


def test_finalize_and_enqueue_carries_only_cited_raw_source_ids(tmp_path):
    from local_kb.cli import build_vault
    from local_kb.finalize import finalize_and_enqueue
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path)
    queue = DiskQueue(vault.queue)
    result = finalize_and_enqueue(vault, queue, _packet(), _answer())
    job = queue.get(result.job_id)

    assert result.path.is_file()
    assert job.metadata["job_type"] == "derived_update"
    assert job.metadata["raw_source_ids"] == ["src-ok"]
    assert job.metadata["answer_path"] == result.path.relative_to(vault.root).as_posix()
    assert job.source_path == ""
    assert all(result.path.name not in value for value in job.metadata["raw_source_ids"])


def test_wiki_citation_enqueues_its_backing_raw_source_ids(tmp_path):
    from local_kb.cli import build_vault
    from local_kb.finalize import finalize_and_enqueue
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path)
    citation = {
        "path": "20_wiki/work/decisions/choice.md",
        "locator": "Current State",
        "source_ids": ["src-wiki"],
        "evidence_sha256": _packet()["evidence"][1]["evidence_sha256"],
    }
    result = finalize_and_enqueue(vault, DiskQueue(vault.queue), _packet(), _answer([citation]))

    assert result.raw_source_ids == ("src-wiki",)


def test_watcher_never_ingests_derived_answer_as_raw_source(tmp_path):
    from local_kb.cli import build_vault, watch_once
    from local_kb.finalize import finalize_and_enqueue
    from local_kb.queue import DiskQueue
    from local_kb.watcher import StableTracker

    vault = build_vault(tmp_path)
    queue = DiskQueue(vault.queue)
    result = finalize_and_enqueue(vault, queue, _packet(), _answer())

    assert watch_once(vault, StableTracker(0, trusted_root=vault.inbox), set()) == []
    job = queue.get(result.job_id)
    assert job.state == "discovered"
    assert job.attempts == 0


def test_queue_failure_leaves_no_saved_answer(tmp_path):
    from local_kb.cli import build_vault
    from local_kb.finalize import finalize_and_enqueue

    class BrokenQueue:
        def enqueue_derived_update(self, *args, **kwargs):
            raise OSError("queue unavailable")

    vault = build_vault(tmp_path)
    before = set(vault.answers.rglob("*.md"))
    with pytest.raises(OSError, match="queue unavailable"):
        finalize_and_enqueue(vault, BrokenQueue(), _packet(), _answer())
    assert set(vault.answers.rglob("*.md")) == before


def test_queue_after_replace_sync_error_reconciles_exact_committed_job(tmp_path, monkeypatch):
    from local_kb.cli import build_vault
    from local_kb.finalize import finalize_and_enqueue
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path)
    queue = DiskQueue(vault.queue)
    monkeypatch.setattr(queue, "_sync_directory", lambda: (_ for _ in ()).throw(OSError("sync ambiguous")))

    result = finalize_and_enqueue(vault, queue, _packet(), _answer())

    assert result.path.is_file()
    assert queue.get(result.job_id).metadata["answer_path"] == result.path.relative_to(vault.root).as_posix()


def test_answer_after_link_error_reconciles_exact_committed_file(tmp_path, monkeypatch):
    import local_kb.finalize as finalize_module
    from local_kb.cli import build_vault
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path)
    original_link = finalize_module.os.link

    def link_then_error(*args, **kwargs):
        original_link(*args, **kwargs)
        raise OSError("link result ambiguous")

    monkeypatch.setattr(finalize_module.os, "link", link_then_error)
    result = finalize_module.finalize_and_enqueue(vault, DiskQueue(vault.queue), _packet(), _answer())

    assert result.path.is_file()
    assert len(list(vault.answers.rglob("*.md"))) == 1


def test_answer_write_failure_does_not_enqueue(tmp_path, monkeypatch):
    from local_kb.cli import build_vault
    from local_kb.finalize import finalize_and_enqueue
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path)
    queue = DiskQueue(vault.queue)

    def fail_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", fail_write)
    with pytest.raises(OSError, match="disk full"):
        finalize_and_enqueue(vault, queue, _packet(), _answer())
    assert queue.iter_jobs() == []
    assert list(vault.answers.rglob("*.md")) == []


def test_finalize_rejects_symlinked_answers_tree(tmp_path):
    from local_kb.finalize import finalize_answer

    outside = tmp_path / "outside"
    outside.mkdir()
    answers = tmp_path / "30_answers"
    try:
        answers.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(ValueError, match="answer"):
        finalize_answer(tmp_path, _packet(), _answer())
    assert list(outside.iterdir()) == []


def test_finalize_cli_prints_saved_path_and_job_id(tmp_path, capsys):
    from local_kb.cli import build_vault, main

    vault = build_vault(tmp_path)
    packet_path = vault.runtime / "packet.json"
    answer_path = vault.runtime / "answer.json"
    packet_path.write_text(json.dumps(_packet(), ensure_ascii=False), encoding="utf-8")
    answer_path.write_text(json.dumps(_answer(), ensure_ascii=False), encoding="utf-8")

    assert main([
        "finalize", "--vault", str(vault.root),
        "--packet", str(packet_path), "--answer", str(answer_path),
    ]) == 0
    output = capsys.readouterr().out
    assert "Saved answer:" in output
    assert "Queued derived update:" in output


def test_finalize_cli_rejects_symlink_input(tmp_path, capsys):
    from local_kb.cli import build_vault, main

    vault = build_vault(tmp_path)
    real = vault.runtime / "real.json"
    linked = vault.runtime / "linked.json"
    answer = vault.runtime / "answer.json"
    real.write_text(json.dumps(_packet()), encoding="utf-8")
    answer.write_text(json.dumps(_answer()), encoding="utf-8")
    try:
        linked.symlink_to(real)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    assert main([
        "finalize", "--vault", str(vault.root),
        "--packet", str(linked), "--answer", str(answer),
    ]) == 1
    assert "regular file" in capsys.readouterr().err


def test_ingest_rejects_saved_answer_without_moving_or_indexing_it(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.finalize import finalize_answer
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path)
    answer = finalize_answer(vault, _packet(), _answer())
    queue = DiskQueue(vault.queue)
    job = queue.enqueue(answer, job_id="derived-direct")
    catalog = Catalog(vault.index / "catalog.sqlite3")

    with pytest.raises(ValueError, match="derived answer"):
        IngestService(vault, queue, catalog).process(job.job_id, space="work")
    assert answer.is_file()
    catalog.initialize()
    assert catalog.search("採用", {"work"}) == []


def test_ingest_rejects_copied_derived_marker_without_moving_or_indexing_it(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.finalize import finalize_answer
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path)
    generated = finalize_answer(vault, _packet(), _answer())
    copied = vault.inbox / "copied-answer.md"
    copied.write_bytes(generated.read_bytes())
    queue = DiskQueue(vault.queue)
    job = queue.enqueue(copied, job_id="derived-copy")
    catalog = Catalog(vault.index / "catalog.sqlite3")

    with pytest.raises(ValueError, match="derived answer"):
        IngestService(vault, queue, catalog).process(job.job_id, space="work")
    assert copied.is_file()
    catalog.initialize()
    assert catalog.search("採用", {"work"}) == []
