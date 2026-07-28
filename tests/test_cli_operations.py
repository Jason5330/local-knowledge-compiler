import json
from pathlib import Path
import re

from local_kb.catalog import Catalog
from local_kb.cli import build_vault, main, watch_once
from local_kb.config import Config
from local_kb.finalize import finalize_and_enqueue
from local_kb.ingest import IngestService
from local_kb.query import QueryService
from local_kb.queue import DiskQueue
from local_kb.watcher import StableTracker


def _set_provider(paths, provider: str) -> None:
    paths.config.write_text(
        re.sub(
            r'provider = "[^"]+"',
            f'provider = "{provider}"',
            paths.config.read_text(encoding="utf-8"),
            count=1,
        ),
        encoding="utf-8",
    )


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _wiki_compiler(path: str, source_ids: list[str]):
    class FakeCompiler:
        def __init__(self, **kwargs):
            pass

        def compile(self, evidence):
            return {
                "changes": [
                    {
                        "path": path,
                        "title": "Resumed knowledge",
                        "type": "decision",
                        "space": "work",
                        "confidence": "high",
                        "source_ids": source_ids,
                        "current_state": "The resumed compiler published this page.",
                        "conflicts": "",
                        "timeline_entry": "Resumed from an actionable handoff.",
                    }
                ]
            }

    return FakeCompiler


def test_status_is_bounded_json_and_does_not_modify_vault(tmp_path, capsys):
    paths = build_vault(tmp_path)
    queue = DiskQueue(paths.queue)
    source = paths.inbox / "waiting.md"
    source.write_text("waiting", encoding="utf-8")
    job = queue.enqueue(source)
    before = _snapshot_tree(paths.root)

    result = main(["status", "--vault", str(paths.root)])

    report = json.loads(capsys.readouterr().out)
    assert result == 2
    assert report == {
        "schema_version": 1,
        "healthy": False,
        "attention_required": True,
        "truncated": False,
        "actionable_count": 1,
        "actionable_count_is_lower_bound": False,
        "jobs": [
            {
                "job_id": job.job_id,
                "type": "source_ingest",
                "state": "discovered",
                "error": None,
                "handoff_path": None,
                "source_id": None,
                "version_id": None,
            }
        ],
    }
    assert _snapshot_tree(paths.root) == before


def test_status_with_no_actionable_jobs_is_healthy(tmp_path, capsys):
    paths = build_vault(tmp_path)
    before = _snapshot_tree(paths.root)

    result = main(["status", "--vault", str(paths.root)])

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["healthy"] is True
    assert report["attention_required"] is False
    assert report["actionable_count"] == 0
    assert report["actionable_count_is_lower_bound"] is False
    assert report["jobs"] == []
    assert _snapshot_tree(paths.root) == before


def test_readonly_queue_scan_caps_total_entries_not_only_json(tmp_path):
    paths = build_vault(tmp_path)
    for index in range(6):
        (paths.queue / f"noise-{index}.txt").write_text(
            "noise", encoding="utf-8"
        )
    queue = DiskQueue(paths.queue)

    jobs, truncated = queue.iter_jobs_bounded_readonly(
        max_jobs=10,
        max_entries=2,
    )

    assert jobs == []
    assert truncated is True


def test_status_reports_truncated_count_as_lower_bound_without_writes(
    tmp_path, monkeypatch, capsys
):
    import local_kb.cli as cli_module

    paths = build_vault(tmp_path)
    for index in range(6):
        (paths.queue / f"noise-{index}.txt").write_text(
            "noise", encoding="utf-8"
        )
    before = _snapshot_tree(paths.root)
    monkeypatch.setattr(cli_module, "STATUS_MAX_ENTRIES", 2)

    result = main(["status", "--vault", str(paths.root)])

    report = json.loads(capsys.readouterr().out)
    assert result == 2
    assert report["truncated"] is True
    assert report["attention_required"] is True
    assert report["healthy"] is False
    assert report["actionable_count"] == 0
    assert report["actionable_count_is_lower_bound"] is True
    assert report["jobs"] == []
    assert _snapshot_tree(paths.root) == before


def test_ingest_once_missing_claude_reports_handoff_and_exit_two(
    tmp_path, monkeypatch, capsys
):
    import local_kb.compiler as compiler_module

    paths = build_vault(tmp_path)
    source = paths.inbox / "source.md"
    source.write_text("A local decision", encoding="utf-8")

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("claude is unavailable")

    monkeypatch.setattr(compiler_module, "_run_bounded_process", unavailable)

    result = main(
        ["ingest-once", str(paths.root), str(source), "--space", "work"]
    )

    job = DiskQueue(paths.queue).iter_jobs()[0]
    output = capsys.readouterr().out
    assert result == 2
    assert "需要人工處理" in output
    assert "Claude 不可用" in output
    assert job.job_id in output
    assert str(job.metadata["compiler_handoff"]) in output
    assert job.state == "pending_attention"


def test_resume_normal_job_uses_configured_compiler_and_publishes(
    tmp_path, monkeypatch, capsys
):
    import local_kb.cli as cli_module

    paths = build_vault(tmp_path)
    _set_provider(paths, "manual")
    source = paths.inbox / "source.md"
    source.write_text("A local decision", encoding="utf-8")
    assert main(
        ["ingest-once", str(paths.root), str(source), "--space", "work"]
    ) == 2
    capsys.readouterr()
    queue = DiskQueue(paths.queue)
    job = queue.iter_jobs()[0]
    source_id = str(job.metadata["source"]["source_id"])
    _set_provider(paths, "claude")
    monkeypatch.setattr(
        cli_module,
        "ClaudeCompiler",
        _wiki_compiler("20_wiki/work/decisions/resumed.md", [source_id]),
    )

    result = main(
        ["resume", "--vault", str(paths.root), "--job-id", job.job_id]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "published" in output
    assert job.job_id in output
    assert queue.get(job.job_id).state == "published"
    assert (paths.wiki / "work" / "decisions" / "resumed.md").is_file()


def test_resume_failure_returns_one_and_records_queue_retry(
    tmp_path, capsys
):
    paths = build_vault(tmp_path)
    source = paths.inbox / "not-ready.md"
    source.write_text("not ready", encoding="utf-8")
    queue = DiskQueue(paths.queue)
    job = queue.enqueue(source)

    result = main(
        ["resume", "--vault", str(paths.root), "--job-id", job.job_id]
    )

    failed = queue.get(job.job_id)
    assert result == 1
    assert failed.state == "retrying"
    assert failed.attempts == 1
    assert "awaiting a compiler handoff" in str(failed.error)
    assert "kb:" in capsys.readouterr().err


def test_resume_derived_job_uses_derived_path_and_never_adds_raw_source(
    tmp_path, monkeypatch, capsys
):
    import local_kb.cli as cli_module

    paths = build_vault(tmp_path)
    _set_provider(paths, "manual")
    queue = DiskQueue(paths.queue)
    catalog = Catalog(paths.index / "catalog.sqlite3")
    source = paths.inbox / "proof.md"
    source.write_text("The approved choice is B.", encoding="utf-8")
    raw = IngestService(paths, queue, catalog).process(
        queue.enqueue(source).job_id, space="work"
    )
    packet = QueryService(catalog, vault=paths, queue=queue).prepare(
        "What choice was approved?", {"work"}
    )
    evidence = next(
        item for item in packet["evidence"] if item["kind"] == "raw_fragment"
    )
    result = finalize_and_enqueue(
        paths,
        queue,
        packet,
        {
            "conclusion": "The approved choice is B.",
            "citations": [
                {
                    key: evidence[key]
                    for key in (
                        "source_id",
                        "version_id",
                        "locator",
                        "evidence_sha256",
                    )
                }
            ],
            "confidence": "high",
            "conflicts": "",
        },
    )
    watch_once(
        paths,
        StableTracker(0, trusted_root=paths.inbox),
        set(),
        space="work",
    )
    assert queue.get(result.job_id).state == "pending_attention"
    with catalog.connection() as connection:
        before = int(connection.execute("SELECT count(*) FROM sources").fetchone()[0])
    _set_provider(paths, "claude")
    monkeypatch.setattr(
        cli_module,
        "ClaudeCompiler",
        _wiki_compiler(
            "20_wiki/work/decisions/derived-resumed.md", [raw.source_id]
        ),
    )
    capsys.readouterr()

    exit_code = main(
        ["resume", "--vault", str(paths.root), "--job-id", result.job_id]
    )

    assert exit_code == 0
    assert queue.get(result.job_id).state == "published"
    assert (
        paths.wiki / "work" / "decisions" / "derived-resumed.md"
    ).is_file()
    with catalog.connection() as connection:
        after = int(connection.execute("SELECT count(*) FROM sources").fetchone()[0])
    assert after == before


def test_watch_reports_new_derived_manual_handoff_once(tmp_path, capsys):
    paths = build_vault(tmp_path)
    _set_provider(paths, "manual")
    queue = DiskQueue(paths.queue)
    catalog = Catalog(paths.index / "catalog.sqlite3")
    source = paths.inbox / "proof.md"
    source.write_text("The approved choice is B.", encoding="utf-8")
    raw = IngestService(paths, queue, catalog).process(
        queue.enqueue(source).job_id, space="work"
    )
    packet = QueryService(catalog, vault=paths, queue=queue).prepare(
        "What choice was approved?", {"work"}
    )
    evidence = next(
        item for item in packet["evidence"] if item["kind"] == "raw_fragment"
    )
    finalized = finalize_and_enqueue(
        paths,
        queue,
        packet,
        {
            "conclusion": "The approved choice is B.",
            "citations": [
                {
                    key: evidence[key]
                    for key in (
                        "source_id",
                        "version_id",
                        "locator",
                        "evidence_sha256",
                    )
                }
            ],
            "confidence": "high",
            "conflicts": "",
        },
    )
    tracker = StableTracker(0, trusted_root=paths.inbox)

    watch_once(paths, tracker, set(), space="work")
    first = capsys.readouterr()
    watch_once(paths, tracker, set(), space="work")
    second = capsys.readouterr()

    pending = queue.get(finalized.job_id)
    assert finalized.job_id in first.err
    assert str(pending.metadata["compiler_handoff"]) in first.err
    assert "pending_attention" in first.err
    assert finalized.job_id not in second.err


def test_watch_reports_new_source_manual_handoff_once(tmp_path, capsys):
    paths = build_vault(tmp_path)
    _set_provider(paths, "manual")
    queue = DiskQueue(paths.queue)
    source = paths.inbox / "source.md"
    source.write_text("A local decision", encoding="utf-8")
    job = queue.enqueue(source)
    tracker = StableTracker(0, trusted_root=paths.inbox)

    watch_once(paths, tracker, set(), space="work")
    first = capsys.readouterr()
    watch_once(paths, tracker, set(), space="work")
    second = capsys.readouterr()

    pending = queue.get(job.job_id)
    assert pending.state == "pending_attention"
    assert job.job_id in first.err
    assert str(pending.metadata["compiler_handoff"]) in first.err
    assert job.job_id not in second.err


def test_beginner_readme_documents_status_resume_and_exit_codes():
    readme = (
        Path(__file__).resolve().parents[1] / "README.md"
    ).read_text(encoding="utf-8")

    assert "kb.exe status --vault" in readme
    assert "kb.exe resume --vault" in readme
    assert "--job-id" in readme
    assert "Exit code `0`" in readme
    assert "Exit code `1`" in readme
    assert "Exit code `2`" in readme
    assert "pending_attention" in readme


def test_status_discovers_project_local_vault(
    tmp_path, monkeypatch, capsys
):
    project = tmp_path / "project"
    project.mkdir()
    vault = build_vault(project / "KnowledgeBase")
    monkeypatch.chdir(project)

    result = main(["status"])

    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["healthy"] is True
    assert vault.root == (project / "KnowledgeBase").resolve()


def test_explicit_status_vault_overrides_project_child(
    tmp_path, monkeypatch, capsys
):
    project = tmp_path / "project"
    project.mkdir()
    build_vault(project / "KnowledgeBase")
    explicit = build_vault(tmp_path / "explicit")
    queue = DiskQueue(explicit.queue)
    queue.enqueue(explicit.inbox / "missing-source.md")
    monkeypatch.chdir(project)

    result = main(["status", "--vault", str(explicit.root)])

    assert result == 2
    report = json.loads(capsys.readouterr().out)
    assert report["actionable_count"] == 1
