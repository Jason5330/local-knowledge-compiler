import json
import io
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import pytest


class _RecordingInput(io.BytesIO):
    def close(self):
        self.flush()


class _FakePopen:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, running=False):
        self.stdin = _RecordingInput()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None if running else returncode
        self.final_returncode = returncode
        self.pid = 4242
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(["claude"], timeout)
        return self.returncode

    def terminate(self):
        self.killed = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


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

    process = _FakePopen(stdout=b'{"result":{"changes":[]}}')

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr("local_kb.compiler.subprocess.Popen", fake_popen)
    compiler = ClaudeCompiler(
        fallback=ManualCompiler(tmp_path / "manual", trusted_root=tmp_path), cwd=tmp_path,
    )

    assert compiler.compile("evidence") == {"changes": []}
    assert "--tools" in captured["command"]
    assert captured["command"][captured["command"].index("--tools") + 1] == ""
    assert process.stdin.getvalue().decode("utf-8").endswith("\n\nevidence")
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["cwd"] == str(tmp_path)


def test_claude_compiler_writes_manual_handoff_when_cli_is_unavailable(monkeypatch, tmp_path):
    from local_kb.compiler import ClaudeCompiler, ManualCompiler

    def fail(*_args, **_kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr("local_kb.compiler.subprocess.Popen", fail)
    path = ClaudeCompiler(
        fallback=ManualCompiler(tmp_path / "manual", trusted_root=tmp_path)
    ).compile("evidence")

    packet = json.loads(path.read_text(encoding="utf-8"))
    assert path.suffix == ".json"
    assert packet["status"] == "needs_agent"
    assert packet["evidence"] == "evidence"
    assert "reason" in packet


def test_claude_compiler_falls_back_for_nonzero_or_malformed_output(monkeypatch, tmp_path):
    from local_kb.compiler import ClaudeCompiler, ManualCompiler

    process = _FakePopen(stdout=b"not json", stderr=b"bad", returncode=3)
    monkeypatch.setattr("local_kb.compiler.subprocess.Popen", lambda *_args, **_kwargs: process)
    path = ClaudeCompiler(
        fallback=ManualCompiler(tmp_path / "manual", trusted_root=tmp_path)
    ).compile("evidence")

    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "needs_agent"


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_claude_compiler_kills_process_when_stream_output_exceeds_hard_limit(monkeypatch, tmp_path, stream):
    from local_kb.compiler import ClaudeCompiler, MAX_OUTPUT_BYTES, ManualCompiler

    payload = b"x" * (MAX_OUTPUT_BYTES + 100_000)
    process = _FakePopen(
        stdout=payload if stream == "stdout" else b"",
        stderr=payload if stream == "stderr" else b"",
        running=True,
    )
    monkeypatch.setattr("local_kb.compiler.subprocess.Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        "local_kb.compiler._terminate_process_tree",
        lambda child: child.kill(),
    )

    path = ClaudeCompiler(
        fallback=ManualCompiler(tmp_path / "manual", trusted_root=tmp_path), timeout=1,
    ).compile("evidence")

    assert process.killed is True
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "needs_agent"


def test_claude_compiler_timeout_kills_process_tree_and_falls_back(monkeypatch, tmp_path):
    from local_kb.compiler import ClaudeCompiler, ManualCompiler

    process = _FakePopen(running=True)
    monkeypatch.setattr("local_kb.compiler.subprocess.Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        "local_kb.compiler._terminate_process_tree",
        lambda child: child.kill(),
    )

    path = ClaudeCompiler(
        fallback=ManualCompiler(tmp_path / "manual", trusted_root=tmp_path), timeout=0.02,
    ).compile("evidence")

    assert process.killed is True
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "needs_agent"


def test_bounded_process_drains_quick_exit_stdout_to_eof(tmp_path):
    from local_kb.compiler import _run_bounded_process

    expected = b'{"result":{"changes":[]}}TAIL'
    command = [sys.executable, "-c", f"import sys;sys.stdout.buffer.write({expected!r})"]

    returncode, stdout, overflowed, timed_out = _run_bounded_process(
        command, "prompt", cwd=tmp_path, timeout=5,
    )

    assert returncode == 0
    assert stdout == expected
    assert overflowed is False
    assert timed_out is False


def test_bounded_process_preserves_tail_of_near_limit_fast_output(tmp_path):
    from local_kb.compiler import MAX_OUTPUT_BYTES, _run_bounded_process

    count = MAX_OUTPUT_BYTES - 100
    command = [
        sys.executable,
        "-c",
        f"import sys;sys.stdout.buffer.write(b'x'*{count}+b'TAIL')",
    ]

    returncode, stdout, overflowed, timed_out = _run_bounded_process(
        command, "prompt", cwd=tmp_path, timeout=5,
    )

    assert returncode == 0
    assert len(stdout) == count + 4
    assert stdout.endswith(b"TAIL")
    assert overflowed is False
    assert timed_out is False


def test_bounded_process_supports_custom_limit_and_empty_stdin(tmp_path):
    from local_kb.compiler import _run_bounded_process

    command = [
        sys.executable,
        "-c",
        "import sys; assert sys.stdin.buffer.read() == b''; sys.stdout.buffer.write(b'x' * 1025)",
    ]

    returncode, stdout, overflowed, timed_out = _run_bounded_process(
        command, None, cwd=tmp_path, timeout=5, max_output_bytes=1024,
    )

    assert isinstance(returncode, int)
    assert stdout == b"x" * 1024
    assert overflowed is True
    assert timed_out is False


def test_manual_compiler_writes_complete_unique_no_clobber_handoffs(tmp_path):
    from local_kb.compiler import ManualCompiler

    compiler = ManualCompiler(tmp_path / "manual", trusted_root=tmp_path)
    first = compiler.compile("evidence")
    second = compiler.compile("evidence")

    assert first != second
    assert first.exists() and second.exists()
    packet = json.loads(first.read_text(encoding="utf-8"))
    assert packet["status"] == "needs_agent"
    assert packet["schema_version"] == 1
    assert packet["output_schema"]["required"] == ["changes"]


def test_manual_compiler_single_argument_uses_absolute_outbox_parent_as_trusted_root(tmp_path):
    from local_kb.compiler import ManualCompiler

    outbox = (tmp_path / "manual").absolute()
    handoff = ManualCompiler(outbox).compile("single argument evidence")

    assert handoff.parent == outbox
    assert json.loads(handoff.read_text(encoding="utf-8"))["evidence"] == "single argument evidence"


def test_manual_compiler_rejects_posix_symlinked_outbox_ancestor_without_writing_outside(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX symlink test")
    from local_kb.compiler import ManualCompiler

    trusted = tmp_path / "trusted"
    outside = tmp_path / "outside"
    trusted.mkdir()
    outside.mkdir()
    (trusted / "redirect").symlink_to(outside, target_is_directory=True)

    with pytest.raises((OSError, ValueError), match="safe|link|trusted"):
        ManualCompiler(
            trusted / "redirect" / "manual", trusted_root=trusted,
        ).compile("secret evidence")
    with pytest.raises((OSError, ValueError), match="safe|link|trusted|directory"):
        ManualCompiler(trusted / "redirect" / "manual").compile("secret evidence")
    assert list(outside.iterdir()) == []


def test_manual_compiler_rejects_windows_junction_without_writing_outside(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows junction test")
    from local_kb.compiler import ManualCompiler

    trusted = tmp_path / "trusted"
    outside = tmp_path / "outside"
    trusted.mkdir()
    outside.mkdir()
    junction = trusted / "redirect"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        pytest.skip("junctions unavailable")
    with pytest.raises((OSError, ValueError), match="safe|reparse|trusted"):
        ManualCompiler(
            junction / "manual", trusted_root=trusted,
        ).compile("secret evidence")
    with pytest.raises((OSError, ValueError), match="safe|reparse|trusted"):
        ManualCompiler(junction / "manual").compile("secret evidence")
    assert list(outside.iterdir()) == []


def test_manual_compiler_pinned_outbox_does_not_follow_replacement_race(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX dir_fd race test")
    from local_kb.compiler import ManualCompiler

    trusted = tmp_path / "trusted"
    outside = tmp_path / "outside"
    outbox = trusted / "runtime" / "manual"
    moved = trusted / "pinned-original"
    trusted.mkdir()
    outside.mkdir()
    original_link = os.link
    raced = False

    def replace_then_link(source, target, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            outbox.rename(moved)
            outbox.symlink_to(outside, target_is_directory=True)
        return original_link(source, target, **kwargs)

    monkeypatch.setattr("local_kb.compiler.os.link", replace_then_link)
    ManualCompiler(outbox, trusted_root=trusted).compile("secret evidence")

    assert raced is True
    assert list(outside.iterdir()) == []
    packets = list(moved.glob("manual_*.json"))
    assert len(packets) == 1
    assert "secret evidence" in packets[0].read_text(encoding="utf-8")


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
    record = pending.metadata["compiler_handoffs"][0]
    assert len(record["sha256"]) == 64
    assert len(record["identity"]) == 4
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
    manual = ManualCompiler(vault.runtime / "manual", trusted_root=vault.root)

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
    manual = ManualCompiler(vault.runtime / "manual", trusted_root=vault.root)

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


@pytest.mark.parametrize("field, replacement", [
    ("evidence", "tampered evidence"),
    ("output_schema", {"type": "tampered"}),
])
def test_unpersisted_handoff_cleanup_preserves_packet_changed_after_hash_binding(tmp_path, field, replacement):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.compiler import ManualCompiler
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue)
    manual = ManualCompiler(vault.runtime / "manual", trusted_root=vault.root)

    class FailAndTamperMarker:
        def compile(self, evidence):
            packet = manual.compile(evidence)
            original_write = queue._write

            def fail_once(*_args, **_kwargs):
                payload = json.loads(packet.read_text(encoding="utf-8"))
                payload[field] = replacement
                packet.write_text(json.dumps(payload), encoding="utf-8")
                queue._write = original_write
                raise OSError("marker write failed")

            queue._write = fail_once
            return packet

    service = IngestService(vault, queue, Catalog(vault.index / "catalog.sqlite3"), compiler=FailAndTamperMarker())
    incoming = vault.inbox / "note.txt"
    incoming.write_text("hash binding", encoding="utf-8")
    job = queue.enqueue(incoming, job_id=f"tamper-{field}")

    with pytest.raises(OSError, match="marker write failed"):
        service.process(job.job_id)
    packets = list((vault.runtime / "manual").glob("manual_*.json"))
    assert len(packets) == 1
    assert json.loads(packets[0].read_text(encoding="utf-8"))[field] == replacement


def test_unpersisted_handoff_cleanup_preserves_replaced_packet_identity(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.compiler import ManualCompiler
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue)
    manual = ManualCompiler(vault.runtime / "manual", trusted_root=vault.root)

    class FailAndReplaceMarker:
        def compile(self, evidence):
            packet = manual.compile(evidence)
            original_write = queue._write

            def fail_once(*_args, **_kwargs):
                payload = json.loads(packet.read_text(encoding="utf-8"))
                payload["evidence"] = "replacement evidence"
                replacement = packet.with_suffix(".replacement")
                replacement.write_text(json.dumps(payload), encoding="utf-8")
                os.replace(replacement, packet)
                queue._write = original_write
                raise OSError("marker replace failed")

            queue._write = fail_once
            return packet

    service = IngestService(vault, queue, Catalog(vault.index / "catalog.sqlite3"), compiler=FailAndReplaceMarker())
    incoming = vault.inbox / "note.txt"
    incoming.write_text("identity binding", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="replace-packet")

    with pytest.raises(OSError, match="marker replace failed"):
        service.process(job.job_id)
    packets = list((vault.runtime / "manual").glob("manual_*.json"))
    assert len(packets) == 1
    assert json.loads(packets[0].read_text(encoding="utf-8"))["evidence"] == "replacement evidence"


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


def test_process_replays_durable_receipt_without_calling_model_after_publish_marker_failure(
    tmp_path, monkeypatch
):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue
    from local_kb.transaction import ChangeTransaction

    class DriftingCompiler:
        calls = 0

        def compile(self, evidence):
            self.calls += 1
            source_id = evidence.split(" locator=", 1)[0].removeprefix("source_id=")
            name = "first" if self.calls == 1 else "second"
            return {"changes": [_valid_change(
                path=f"20_wiki/work/{name}.md",
                title=name.title(),
                source_ids=[source_id],
            )]}

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue, max_retries=5)
    compiler = DriftingCompiler()
    service = IngestService(vault, queue, Catalog(vault.index / "catalog.sqlite3"), compiler=compiler)
    incoming = vault.inbox / "note.txt"
    incoming.write_text("stable receipt", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="receipt-replay")
    original_commit = ChangeTransaction.commit_git

    def commit_then_fail_marker(transaction, message, paths=None):
        committed = original_commit(transaction, message, paths=paths)
        original_write = queue._write

        def fail_once(*_args, **_kwargs):
            queue._write = original_write
            raise OSError("complete marker")

        queue._write = fail_once
        return committed

    monkeypatch.setattr(ChangeTransaction, "commit_git", commit_then_fail_marker)
    with pytest.raises(OSError, match="complete marker"):
        service.process(job.job_id, space="work")
    first_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=vault.root, text=True, capture_output=True, check=True,
    ).stdout.strip()

    retrying = queue.get(job.job_id)
    assert retrying.metadata["compiler_status"] == "ready"
    assert "compilation_receipt" in retrying.metadata
    monkeypatch.setattr(ChangeTransaction, "commit_git", original_commit)
    service.process(job.job_id, space="work")

    completed = queue.get(job.job_id)
    assert compiler.calls == 1
    assert completed.state == "published"
    assert completed.metadata["compiler_status"] == "completed"
    assert (vault.wiki / "work" / "first.md").is_file()
    assert not (vault.wiki / "work" / "second.md").exists()
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=vault.root, text=True, capture_output=True, check=True,
    ).stdout.strip() == first_head


def test_process_reconciles_git_after_publish_succeeded_but_first_commit_failed(
    tmp_path, monkeypatch
):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue
    from local_kb.transaction import ChangeTransaction

    class Compiler:
        calls = 0

        def compile(self, evidence):
            self.calls += 1
            source_id = evidence.split(" locator=", 1)[0].removeprefix("source_id=")
            return {"changes": [_valid_change(
                path="20_wiki/work/only.md", title="Only", source_ids=[source_id],
            )]}

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue, max_retries=5)
    compiler = Compiler()
    service = IngestService(vault, queue, Catalog(vault.index / "catalog.sqlite3"), compiler=compiler)
    incoming = vault.inbox / "note.txt"
    incoming.write_text("git reconcile", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="git-reconcile")
    original_commit = ChangeTransaction.commit_git
    attempts = 0

    def fail_first_commit(transaction, message, paths=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated git failure")
        return original_commit(transaction, message, paths=paths)

    monkeypatch.setattr(ChangeTransaction, "commit_git", fail_first_commit)
    with pytest.raises(RuntimeError, match="simulated git failure"):
        service.process(job.job_id, space="work")
    assert (vault.wiki / "work" / "only.md").is_file()
    assert not (vault.root / ".git").exists()
    assert list(vault.staging.iterdir()) == []
    user_note = vault.wiki / "work" / "user-note.md"
    staged_note = vault.wiki / "work" / "staged-note.md"
    user_note.write_text("private untracked draft", encoding="utf-8")
    staged_note.write_text("private staged draft", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=vault.root, capture_output=True, check=True)
    subprocess.run(
        ["git", "add", "20_wiki/work/staged-note.md"],
        cwd=vault.root, capture_output=True, check=True,
    )

    service.process(job.job_id, space="work")

    expected = (vault.wiki / "work" / "only.md").read_bytes()
    committed = subprocess.run(
        ["git", "show", "HEAD:20_wiki/work/only.md"],
        cwd=vault.root, capture_output=True, check=True,
    ).stdout
    assert committed == expected
    assert compiler.calls == 1
    assert attempts == 2
    assert queue.get(job.job_id).state == "published"
    assert list(vault.staging.iterdir()) == []
    assert subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=vault.root, text=True, capture_output=True, check=True,
    ).stdout.splitlines() == ["20_wiki/work/only.md"]
    assert "20_wiki/work/user-note.md" in subprocess.run(
        ["git", "status", "--short"], cwd=vault.root, text=True, capture_output=True, check=True,
    ).stdout
    assert subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=vault.root, text=True, capture_output=True, check=True,
    ).stdout.splitlines() == ["20_wiki/work/staged-note.md"]


def test_process_does_not_complete_while_git_commit_keeps_failing(tmp_path, monkeypatch):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue
    from local_kb.transaction import ChangeTransaction

    class Compiler:
        def compile(self, evidence):
            source_id = evidence.split(" locator=", 1)[0].removeprefix("source_id=")
            return {"changes": [_valid_change(source_ids=[source_id])]}

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue, max_retries=5)
    service = IngestService(vault, queue, Catalog(vault.index / "catalog.sqlite3"), compiler=Compiler())
    incoming = vault.inbox / "note.txt"
    incoming.write_text("persistent git failure", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="git-always-fails")
    monkeypatch.setattr(
        ChangeTransaction, "commit_git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("git stays down")),
    )

    with pytest.raises(RuntimeError, match="git stays down"):
        service.process(job.job_id)
    with pytest.raises(RuntimeError, match="git stays down"):
        service.process(job.job_id)

    current = queue.get(job.job_id)
    assert current.state != "published"
    assert current.metadata["compiler_status"] == "ready"
    assert list(vault.staging.iterdir()) == []


@pytest.mark.parametrize("git_state", ["missing_head", "untracked", "wrong_head", "ignored"])
def test_process_rejects_unverified_git_head_for_receipt(tmp_path, monkeypatch, git_state):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue
    from local_kb.transaction import ChangeTransaction

    class Compiler:
        def compile(self, evidence):
            source_id = evidence.split(" locator=", 1)[0].removeprefix("source_id=")
            return {"changes": [_valid_change(source_ids=[source_id])]}

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue, max_retries=5)
    service = IngestService(vault, queue, Catalog(vault.index / "catalog.sqlite3"), compiler=Compiler())
    incoming = vault.inbox / "note.txt"
    incoming.write_text(f"bad head {git_state}", encoding="utf-8")
    job = queue.enqueue(incoming, job_id=f"bad-head-{git_state}")
    monkeypatch.setattr(
        ChangeTransaction, "commit_git",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("first commit failed")),
    )
    with pytest.raises(RuntimeError, match="first commit failed"):
        service.process(job.job_id)

    page = vault.wiki / "work" / "testing.md"
    expected = page.read_bytes()
    if git_state != "missing_head":
        subprocess.run(["git", "init"], cwd=vault.root, capture_output=True, check=True)
    if git_state == "wrong_head":
        page.write_text("wrong committed bytes", encoding="utf-8")
        subprocess.run(
            ["git", "add", "20_wiki/work/testing.md"], cwd=vault.root, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@local",
             "-c", "commit.gpgsign=false", "commit", "-m", "wrong"],
            cwd=vault.root, capture_output=True, check=True,
        )
        page.write_bytes(expected)
    elif git_state == "ignored":
        (vault.root / ".gitignore").write_text("20_wiki/\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore"], cwd=vault.root, capture_output=True, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@local",
             "-c", "commit.gpgsign=false", "commit", "-m", "ignore"],
            cwd=vault.root, capture_output=True, check=True,
        )

    monkeypatch.setattr(ChangeTransaction, "commit_git", lambda *_args, **_kwargs: False)
    with pytest.raises(RuntimeError, match="Git HEAD"):
        service.process(job.job_id)
    assert queue.get(job.job_id).state != "published"


def test_empty_compilation_receipt_completes_without_creating_git_repository(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    class EmptyCompiler:
        def compile(self, evidence):
            return {"changes": []}

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue)
    service = IngestService(
        vault, queue, Catalog(vault.index / "catalog.sqlite3"), compiler=EmptyCompiler(),
    )
    incoming = vault.inbox / "note.txt"
    incoming.write_text("insufficient evidence", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="empty-receipt")

    service.process(job.job_id)

    assert queue.get(job.job_id).state == "published"
    assert not (vault.root / ".git").exists()
    assert list(vault.staging.iterdir()) == []


def test_git_receipt_verification_checks_size_before_reading_large_blob(tmp_path, monkeypatch):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path / "vault")
    (vault.root / ".git").mkdir()
    service = IngestService(vault, DiskQueue(vault.queue), Catalog(vault.index / "catalog.sqlite3"))
    expected = b"small receipt page"
    receipt = {"pages": [{
        "path": "20_wiki/work/page.md",
        "content": expected.decode(),
        "sha256": hashlib.sha256(expected).hexdigest(),
    }]}
    commands = []
    head = b"a" * 40 + b"\n"
    blob = b"b" * 40 + b"\n"

    def fake_bounded(command, prompt, **kwargs):
        commands.append(command)
        if command[1:3] == ["rev-parse", "--show-toplevel"]:
            return 0, str(vault.root).encode() + b"\n", False, False
        if command[1:3] == ["rev-parse", "--verify"] and command[-1] == "HEAD^{commit}":
            return 0, head, False, False
        if command[1:3] == ["rev-parse", "--verify"]:
            return 0, blob, False, False
        if command[1:3] == ["cat-file", "-t"]:
            return 0, b"blob\n", False, False
        if command[1:3] == ["cat-file", "-s"]:
            return 0, b"1000000000\n", False, False
        raise AssertionError("blob bytes must not be read after a size mismatch")

    monkeypatch.setattr("local_kb.ingest._run_bounded_process", fake_bounded, raising=False)
    with pytest.raises(RuntimeError, match="size|Git HEAD"):
        service._verify_receipt_git_head(receipt)

    assert any(command[1:3] == ["cat-file", "-s"] for command in commands)
    assert not any(command[1:3] == ["cat-file", "blob"] for command in commands)


def test_git_receipt_verification_fails_if_head_changes_during_bounded_read(tmp_path, monkeypatch):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path / "vault")
    (vault.root / ".git").mkdir()
    service = IngestService(vault, DiskQueue(vault.queue), Catalog(vault.index / "catalog.sqlite3"))
    expected = b"stable bytes"
    receipt = {"pages": [{
        "path": "20_wiki/work/page.md",
        "content": expected.decode(),
        "sha256": hashlib.sha256(expected).hexdigest(),
    }]}
    head_reads = 0

    def fake_bounded(command, prompt, **kwargs):
        nonlocal head_reads
        if command[1:3] == ["rev-parse", "--show-toplevel"]:
            return 0, str(vault.root).encode() + b"\n", False, False
        if command[1:3] == ["rev-parse", "--verify"] and command[-1] == "HEAD^{commit}":
            head_reads += 1
            value = b"a" * 40 if head_reads == 1 else b"c" * 40
            return 0, value + b"\n", False, False
        if command[1:3] == ["rev-parse", "--verify"]:
            return 0, b"b" * 40 + b"\n", False, False
        if command[1:3] == ["cat-file", "-t"]:
            return 0, b"blob\n", False, False
        if command[1:3] == ["cat-file", "-s"]:
            return 0, str(len(expected)).encode() + b"\n", False, False
        if command[1:3] == ["cat-file", "blob"]:
            return 0, expected, False, False
        raise AssertionError(command)

    monkeypatch.setattr("local_kb.ingest._run_bounded_process", fake_bounded, raising=False)
    with pytest.raises(RuntimeError, match="changed"):
        service._verify_receipt_git_head(receipt)
    assert head_reads == 2


@pytest.mark.parametrize("failure", ["nonzero", "overflow", "timeout"])
def test_git_receipt_verification_fails_closed_for_bounded_git_errors(
    tmp_path, monkeypatch, failure
):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path / "vault")
    (vault.root / ".git").mkdir()
    service = IngestService(vault, DiskQueue(vault.queue), Catalog(vault.index / "catalog.sqlite3"))
    receipt = {"pages": [{
        "path": "20_wiki/work/page.md", "content": "x",
        "sha256": hashlib.sha256(b"x").hexdigest(),
    }]}
    result = {
        "nonzero": (1, b"", False, False),
        "overflow": (0, b"x" * 32, True, False),
        "timeout": (-1, b"", False, True),
    }[failure]
    monkeypatch.setattr(
        "local_kb.ingest._run_bounded_process",
        lambda *_args, **_kwargs: result,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="Git"):
        service._verify_receipt_git_head(receipt)


def test_git_receipt_verification_streams_exact_two_megabyte_blob(tmp_path):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue

    vault = build_vault(tmp_path / "vault")
    content = b"x" * 2_000_000
    page = vault.wiki / "work" / "large.md"
    page.parent.mkdir(exist_ok=True)
    page.write_bytes(content)
    subprocess.run(["git", "init"], cwd=vault.root, capture_output=True, check=True)
    subprocess.run(["git", "add", "20_wiki/work/large.md"], cwd=vault.root, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@local",
         "-c", "commit.gpgsign=false", "commit", "-m", "large"],
        cwd=vault.root, capture_output=True, check=True,
    )
    service = IngestService(vault, DiskQueue(vault.queue), Catalog(vault.index / "catalog.sqlite3"))
    receipt = {"pages": [{
        "path": "20_wiki/work/large.md",
        "content": content.decode(),
        "sha256": hashlib.sha256(content).hexdigest(),
    }]}

    service._verify_receipt_git_head(receipt)


def test_process_fails_closed_when_durable_compilation_receipt_is_tampered(tmp_path, monkeypatch):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue
    from local_kb.transaction import ChangeTransaction

    class Compiler:
        calls = 0

        def compile(self, evidence):
            self.calls += 1
            source_id = evidence.split(" locator=", 1)[0].removeprefix("source_id=")
            return {"changes": [_valid_change(source_ids=[source_id])]}

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue, max_retries=5)
    compiler = Compiler()
    service = IngestService(vault, queue, Catalog(vault.index / "catalog.sqlite3"), compiler=compiler)
    incoming = vault.inbox / "note.txt"
    incoming.write_text("tamper receipt", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="receipt-tamper")
    original_commit = ChangeTransaction.commit_git

    def commit_then_fail_marker(transaction, message, paths=None):
        result = original_commit(transaction, message, paths=paths)
        original_write = queue._write

        def fail_once(*_args, **_kwargs):
            queue._write = original_write
            raise OSError("complete marker")

        queue._write = fail_once
        return result

    monkeypatch.setattr(ChangeTransaction, "commit_git", commit_then_fail_marker)
    with pytest.raises(OSError):
        service.process(job.job_id)
    monkeypatch.setattr(ChangeTransaction, "commit_git", original_commit)

    def tamper(current):
        current.metadata["compilation_receipt"]["pages"][0]["content"] += "tampered"

    queue.update(job.job_id, tamper)
    with pytest.raises(ValueError, match="receipt"):
        service.process(job.job_id)
    assert compiler.calls == 1


def test_resume_replays_durable_receipt_without_reinvoking_replacement_compiler(tmp_path, monkeypatch):
    from local_kb.catalog import Catalog
    from local_kb.cli import build_vault
    from local_kb.ingest import IngestService
    from local_kb.queue import DiskQueue
    from local_kb.transaction import ChangeTransaction

    class Compiler:
        calls = 0

        def compile(self, evidence):
            self.calls += 1
            source_id = evidence.split(" locator=", 1)[0].removeprefix("source_id=")
            name = "first" if self.calls == 1 else "second"
            return {"changes": [_valid_change(
                path=f"20_wiki/work/{name}.md", title=name, source_ids=[source_id],
            )]}

    vault = build_vault(tmp_path / "vault")
    queue = DiskQueue(vault.queue)
    service = IngestService(vault, queue, Catalog(vault.index / "catalog.sqlite3"))
    incoming = vault.inbox / "note.txt"
    incoming.write_text("resume receipt", encoding="utf-8")
    job = queue.enqueue(incoming, job_id="resume-receipt")
    service.process(job.job_id)
    compiler = Compiler()
    original_commit = ChangeTransaction.commit_git

    def commit_then_fail_marker(transaction, message, paths=None):
        result = original_commit(transaction, message, paths=paths)
        original_write = queue._write

        def fail_once(*_args, **_kwargs):
            queue._write = original_write
            raise OSError("resume complete marker")

        queue._write = fail_once
        return result

    monkeypatch.setattr(ChangeTransaction, "commit_git", commit_then_fail_marker)
    with pytest.raises(OSError, match="resume complete marker"):
        service.resume_compilation(job.job_id, compiler=compiler)
    monkeypatch.setattr(ChangeTransaction, "commit_git", original_commit)

    service.resume_compilation(job.job_id, compiler=compiler)
    assert compiler.calls == 1
    assert queue.get(job.job_id).state == "published"
    assert (vault.wiki / "work" / "first.md").is_file()
    assert not (vault.wiki / "work" / "second.md").exists()
