import json
from pathlib import Path
import subprocess

import pytest


def _valid_change(**overrides):
    change = {
        "path": "20_wiki/work/testing.md",
        "title": "Testing",
        "type": "topic",
        "space": "work",
        "confidence": "high",
        "source_ids": ["src_123"],
        "current_state": "Only the supplied evidence supports this statement.",
        "conflicts": "",
        "timeline_entry": "Compiled from the supplied source.",
    }
    change.update(overrides)
    return change


def test_claude_compiler_disables_tools_uses_stdin_and_requires_json(monkeypatch, tmp_path):
    from local_kb.compiler import ClaudeCompiler, ManualCompiler

    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs

        class Result:
            stdout = '{"result":{"changes":[]}}'
            returncode = 0

        return Result()

    monkeypatch.setattr("local_kb.compiler.subprocess.run", fake_run)
    compiler = ClaudeCompiler(fallback=ManualCompiler(tmp_path / "manual"), cwd=tmp_path)

    assert compiler.compile("evidence") == {"changes": []}
    assert "--tools" in captured["command"]
    assert captured["command"][captured["command"].index("--tools") + 1] == ""
    assert captured["kwargs"]["input"].endswith("\n\nevidence")
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["cwd"] == str(tmp_path)


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("claude"),
        subprocess.TimeoutExpired(["claude"], 1),
    ],
)
def test_claude_compiler_writes_manual_handoff_when_cli_is_unavailable(monkeypatch, tmp_path, failure):
    from local_kb.compiler import ClaudeCompiler, ManualCompiler

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr("local_kb.compiler.subprocess.run", fail)
    path = ClaudeCompiler(fallback=ManualCompiler(tmp_path / "manual")).compile("evidence")

    packet = json.loads(path.read_text(encoding="utf-8"))
    assert path.suffix == ".json"
    assert packet["status"] == "needs_agent"
    assert packet["evidence"] == "evidence"
    assert "reason" in packet


def test_claude_compiler_falls_back_for_nonzero_or_malformed_output(monkeypatch, tmp_path):
    from local_kb.compiler import ClaudeCompiler, ManualCompiler

    class Result:
        returncode = 3
        stdout = "not json"
        stderr = "bad"

    monkeypatch.setattr("local_kb.compiler.subprocess.run", lambda *_args, **_kwargs: Result())
    path = ClaudeCompiler(fallback=ManualCompiler(tmp_path / "manual")).compile("evidence")

    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "needs_agent"


def test_manual_compiler_writes_complete_unique_no_clobber_handoffs(tmp_path):
    from local_kb.compiler import ManualCompiler

    compiler = ManualCompiler(tmp_path / "manual")
    first = compiler.compile("evidence")
    second = compiler.compile("evidence")

    assert first != second
    assert first.exists() and second.exists()
    packet = json.loads(first.read_text(encoding="utf-8"))
    assert packet["status"] == "needs_agent"
    assert packet["schema_version"] == 1
    assert packet["output_schema"]["required"] == ["changes"]


def test_compile_extraction_publishes_only_valid_current_source_changes(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.models import SourceVersion
    from local_kb.queue import DiskQueue

    class Compiler:
        def compile(self, evidence):
            source_id = evidence.split(" locator=", 1)[0].removeprefix("source_id=")
            return {"changes": [_valid_change(source_ids=[source_id])]}

    vault = build_vault(tmp_path / "vault")
    service = IngestService(vault, DiskQueue(vault.queue), Catalog(vault.index / "catalog.sqlite3"), compiler=Compiler())
    source = SourceVersion("src_123", "ver_123", "work", "note.txt", "10_raw/work/src_123/ver_123/note.txt", "a" * 64, "text/plain", "extracted")
    paths = service.compile_extraction(source, {"status": "extracted", "fragments": [{"locator": "lines:1-1", "text": "evidence"}]})

    assert paths == ["20_wiki/work/testing.md"]
    page = (vault.root / paths[0]).read_text(encoding="utf-8")
    assert 'source_ids:\n  - "src_123"' in page
    assert 'updated_at: "1970-01-01T00:00:00Z"' not in page


@pytest.mark.parametrize(
    "change",
    [
        _valid_change(path="20_wiki/../escape.md"),
        _valid_change(path="20_wiki\\escape.md"),
        _valid_change(path="20_wiki/CON.md"),
        _valid_change(source_ids=["src_fabricated"]),
        _valid_change(source_ids=["src_123", "src_123"]),
        _valid_change(unexpected="no"),
    ],
)
def test_compile_extraction_rejects_invalid_output_before_any_publish(tmp_path, change):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.models import SourceVersion
    from local_kb.queue import DiskQueue

    class Compiler:
        def compile(self, _evidence):
            return {"changes": [_valid_change(path="20_wiki/work/first.md"), change]}

    vault = build_vault(tmp_path / "vault")
    service = IngestService(vault, DiskQueue(vault.queue), Catalog(vault.index / "catalog.sqlite3"), compiler=Compiler())
    source = SourceVersion("src_123", "ver_123", "work", "note.txt", "10_raw/work/src_123/ver_123/note.txt", "a" * 64, "text/plain", "extracted")

    with pytest.raises(ValueError):
        service.compile_extraction(source, {"status": "extracted", "fragments": [{"locator": "lines:1-1", "text": "evidence"}]})
    assert not (vault.wiki / "work" / "first.md").exists()


def test_pending_extractor_never_calls_compiler(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    class PendingRegistry:
        def extract(self, _path):
            from local_kb.extractors.base import Extraction

            return Extraction("pending_extractor", [])

    class Compiler:
        def compile(self, _evidence):
            raise AssertionError("pending extraction must not reach compiler")

    vault = build_vault(tmp_path / "vault")
    service = IngestService(vault, DiskQueue(vault.queue), Catalog(vault.index / "catalog.sqlite3"), registry=PendingRegistry(), compiler=Compiler())
    incoming = vault.inbox / "movie.mp4"
    incoming.write_bytes(b"not a movie")
    job = service.queue.enqueue(incoming, job_id="pending")

    assert service.process(job.job_id).status == "pending_extractor"


def test_default_manual_handoff_finishes_local_ingest_but_leaves_job_pending(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue)
    service = IngestService(vault, queue, Catalog(vault.index / "catalog.sqlite3"))
    incoming = vault.inbox / "note.txt"
    incoming.write_text("durable local evidence", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="manual-handoff")

    source = service.process(job.job_id, space="work")
    pending = queue.get(job.job_id)

    assert source.status == "extracted"
    assert pending.state == "pending_attention"
    assert pending.metadata["compiler_status"] == "needs_agent"
    assert pending.metadata["compiler_handoff"].startswith(".kb/manual/manual_")
    assert len(pending.metadata["compiler_handoffs"]) == 1
    assert (vault.root / pending.metadata["compiler_handoff"]).is_file()
    assert (vault.index / "cache" / f"{source.version_id}.json").is_file()
    assert service.catalog.search("durable", {"work"})


def test_resume_compilation_publishes_only_after_replacement_compiler_succeeds(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    class Compiler:
        def compile(self, evidence):
            source_id = evidence.split(" locator=", 1)[0].removeprefix("source_id=")
            return {"changes": [_valid_change(source_ids=[source_id])]}

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue)
    service = IngestService(vault, queue, Catalog(vault.index / "catalog.sqlite3"))
    incoming = vault.inbox / "note.txt"
    incoming.write_text("resume evidence", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="resume")
    service.process(job.job_id, space="work")
    handoff = queue.get(job.job_id).metadata["compiler_handoff"]

    source = service.resume_compilation(job.job_id, compiler=Compiler())
    completed = queue.get(job.job_id)

    assert source.status == "extracted"
    assert completed.state == "published"
    assert completed.metadata["compiler_status"] == "completed"
    assert "compiler_handoff" not in completed.metadata
    assert completed.metadata["compiler_handoffs"][0]["path"] == handoff
    assert (vault.wiki / "work" / "testing.md").is_file()


def test_resume_compilation_keeps_pending_and_appends_history_on_new_handoff(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue)
    service = IngestService(vault, queue, Catalog(vault.index / "catalog.sqlite3"))
    incoming = vault.inbox / "note.txt"
    incoming.write_text("retry manual", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="retry-manual")
    service.process(job.job_id)
    first = queue.get(job.job_id).metadata["compiler_handoff"]

    service.resume_compilation(job.job_id)
    pending = queue.get(job.job_id)

    assert pending.state == "pending_attention"
    assert pending.metadata["compiler_status"] == "needs_agent"
    assert len(pending.metadata["compiler_handoffs"]) == 2
    assert pending.metadata["compiler_handoff"] != first
    assert (vault.root / pending.metadata["compiler_handoff"]).is_file()


def test_process_does_not_recreate_handoff_for_an_already_pending_job(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue)
    service = IngestService(vault, queue, Catalog(vault.index / "catalog.sqlite3"))
    incoming = vault.inbox / "note.txt"
    incoming.write_text("no duplicate handoff", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="no-duplicate")
    service.process(job.job_id)
    first = queue.get(job.job_id).metadata["compiler_handoff"]

    service.process(job.job_id)
    pending = queue.get(job.job_id)

    assert pending.state == "pending_attention"
    assert pending.metadata["compiler_handoff"] == first
    assert len(pending.metadata["compiler_handoffs"]) == 1


def test_handoff_marker_write_failure_leaves_no_half_metadata_or_orphan_on_retry(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.compiler import ManualCompiler
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue)
    manual = ManualCompiler(vault.runtime / "manual")

    class FailBeforeMarker:
        first = True

        def compile(self, evidence):
            packet = manual.compile(evidence)
            if self.first:
                self.first = False
                original_write = queue._write

                def fail_once(*_args, **_kwargs):
                    queue._write = original_write
                    raise OSError("before handoff marker")

                queue._write = fail_once
            return packet

    service = IngestService(vault, queue, Catalog(vault.index / "catalog.sqlite3"), compiler=FailBeforeMarker())
    incoming = vault.inbox / "note.txt"
    incoming.write_text("atomic handoff", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="marker-before")

    with pytest.raises(OSError, match="before handoff marker"):
        service.process(job.job_id)
    retrying = queue.get(job.job_id)
    assert "compiler_handoff" not in retrying.metadata
    assert list((vault.runtime / "manual").glob("manual_*.json")) == []

    service.process(job.job_id)
    pending = queue.get(job.job_id)
    assert pending.state == "pending_attention"
    assert len(pending.metadata["compiler_handoffs"]) == 1
    assert len(list((vault.runtime / "manual").glob("manual_*.json"))) == 1


def test_handoff_marker_sync_failure_recognizes_already_persisted_pending_state(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.compiler import ManualCompiler
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue)
    manual = ManualCompiler(vault.runtime / "manual")

    class FailAfterMarker:
        def compile(self, evidence):
            packet = manual.compile(evidence)
            original_sync = queue._sync_directory

            def fail_once():
                queue._sync_directory = original_sync
                raise OSError("after handoff marker")

            queue._sync_directory = fail_once
            return packet

    service = IngestService(vault, queue, Catalog(vault.index / "catalog.sqlite3"), compiler=FailAfterMarker())
    incoming = vault.inbox / "note.txt"
    incoming.write_text("durable marker", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="marker-after")

    assert service.process(job.job_id).status == "extracted"
    pending = queue.get(job.job_id)
    assert pending.state == "pending_attention"
    assert len(pending.metadata["compiler_handoffs"]) == 1
    assert len(list((vault.runtime / "manual").glob("manual_*.json"))) == 1


def test_resume_compilation_fails_closed_for_forged_handoff_metadata(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue)
    service = IngestService(vault, queue, Catalog(vault.index / "catalog.sqlite3"))
    incoming = vault.inbox / "note.txt"
    incoming.write_text("safe evidence", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="forged-handoff")
    service.process(job.job_id)
    queue.update(job.job_id, lambda current: current.metadata.update(compiler_handoff="../outside.json"))

    with pytest.raises(ValueError, match="handoff"):
        service.resume_compilation(job.job_id)


def test_claude_prompt_instruction_constant_is_readable_and_local_only():
    from local_kb.compiler import CLAUDE_PROMPT_INSTRUCTIONS

    assert "Use only the following local evidence" in CLAUDE_PROMPT_INSTRUCTIONS
    assert "Do not browse the web" in CLAUDE_PROMPT_INSTRUCTIONS
    assert "source_id" in CLAUDE_PROMPT_INSTRUCTIONS
