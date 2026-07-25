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
        def compile(self, _evidence):
            return {"changes": [_valid_change()]}

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
