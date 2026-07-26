import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.client import HTTPConnection, HTTPSConnection

import pytest

from local_kb.catalog import Catalog
from local_kb.cli import build_vault
from local_kb.config import Config
from local_kb.health import lint
from local_kb.finalize import finalize_and_enqueue
from local_kb.ingest import IngestService
from local_kb.query import QueryService
from local_kb.queue import DiskQueue


def _citation(evidence: dict[str, object]) -> dict[str, object]:
    return {
        key: evidence[key]
        for key in ("source_id", "version_id", "locator", "evidence_sha256")
    }


def _source_count(catalog: Catalog) -> int:
    with catalog.connection() as connection:
        return int(connection.execute("SELECT count(*) FROM sources").fetchone()[0])


def test_offline_ingest_prepare_cited_answer_finalize_and_derived_job(
    tmp_path, monkeypatch
):
    network_attempts = []

    def reject_network(*args, **kwargs):
        network_attempts.append((args, kwargs))
        raise AssertionError("offline knowledge flow attempted network access")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(urllib.request, "urlopen", reject_network)
    monkeypatch.setattr(HTTPConnection, "connect", reject_network)
    monkeypatch.setattr(HTTPSConnection, "connect", reject_network)
    paths = build_vault(tmp_path)
    source = paths.inbox / "decision.md"
    source.write_text(
        "# 採購決策\n團隊選擇 B 方案，因為維護成本較低。\n"
        "參考書籤：https://example.invalid/never-fetch\n",
        encoding="utf-8",
    )
    queue = DiskQueue(paths.queue)
    catalog = Catalog(paths.index / "catalog.sqlite3")
    job = queue.enqueue(source)

    ingested = IngestService(paths, queue, catalog).process(job.job_id, space="work")
    packet = QueryService(catalog, vault=paths, queue=queue).prepare(
        "團隊最後選擇哪個方案？", {"work"}
    )

    assert ingested.status == "extracted"
    assert packet["status"] == "ready"
    assert all(item["space"] == "work" for item in packet["evidence"])
    raw = next(
        item
        for item in packet["evidence"]
        if item["kind"] == "raw_fragment" and "B 方案" in item["text"]
    )
    answer = {
        "conclusion": "團隊選擇 B 方案。",
        "citations": [_citation(raw)],
        "confidence": "high",
        "conflicts": "沒有發現衝突。",
    }
    result = finalize_and_enqueue(paths, queue, packet, answer)
    derived = queue.get(result.job_id)

    assert result.path.is_file()
    assert raw["source_id"] in result.path.read_text(encoding="utf-8")
    assert derived.metadata["job_type"] == "derived_update"
    assert derived.metadata["raw_source_ids"] == [raw["source_id"]]
    assert all(not str(item["path"]).startswith(("http://", "https://"))
               for item in packet["evidence"])
    assert network_attempts == []


def test_init_installs_one_canonical_protocol_and_thin_agent_entries(tmp_path):
    paths = build_vault(tmp_path)

    protocol = (paths.system / "KNOWLEDGE_PROTOCOL.md").read_text(encoding="utf-8")
    agents = (paths.root / "AGENTS.md").read_text(encoding="utf-8")
    claude = (paths.root / "CLAUDE.md").read_text(encoding="utf-8")

    for required in (
        "kb prepare",
        "kb finalize",
        "insufficient_evidence",
        "只使用本地證據",
        "不得搜尋網路",
        "裸網址",
        "不得抓取",
        "space",
        "直接結論",
        "證據整理",
        "來源",
        "衝突與時效",
        "信心",
        "未知事項",
        "下一個本地資料缺口",
    ):
        assert required in protocol
    assert "80_system/KNOWLEDGE_PROTOCOL.md" in agents
    assert "80_system/KNOWLEDGE_PROTOCOL.md" in claude
    for entry in (agents, claude):
        assert "只使用本地證據" in entry
        assert "不得自動搜尋網路" in entry
    assert "kb prepare" not in agents
    assert "kb prepare" not in claude


def test_fresh_init_is_immediately_healthy(tmp_path):
    paths = build_vault(tmp_path)

    report = lint(paths)

    assert report["healthy"] is True
    assert report["issues"]["index_raw_mismatches"] == []


@pytest.mark.parametrize("payload", [b"", b"not a sqlite database"])
def test_init_rejects_existing_empty_or_corrupt_catalog(tmp_path, payload):
    paths = build_vault(tmp_path)
    database = paths.index / "catalog.sqlite3"
    database.write_bytes(payload)

    with pytest.raises(ValueError, match="catalog.*invalid|rebuild"):
        build_vault(tmp_path)


def test_init_rejects_unsafe_sidecar_when_catalog_main_is_missing(tmp_path):
    root = tmp_path / "vault"
    index = root / "40_index"
    index.mkdir(parents=True)
    outside = tmp_path / "outside-shm"
    outside.write_bytes(b"keep-external-bytes")
    os.link(outside, index / "catalog.sqlite3-shm")
    before = outside.read_bytes()

    with pytest.raises(ValueError, match="catalog.*unsafe|single-link"):
        build_vault(root)

    assert outside.read_bytes() == before


def test_sixteen_concurrent_initializers_publish_one_valid_vault(tmp_path):
    root = tmp_path / "vault with spaces 知識庫"
    barrier = threading.Barrier(16)
    results = []

    def initialize():
        try:
            barrier.wait(timeout=10)
            results.append(build_vault(root))
        except BaseException as error:
            results.append(error)

    threads = [threading.Thread(target=initialize) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 16
    errors = [repr(result) for result in results if isinstance(result, BaseException)]
    assert errors == []
    assert lint(results[0])["healthy"] is True
    assert not list(root.rglob("*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction coverage")
def test_init_rejects_parent_junction_before_any_external_write(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    junction = tmp_path / "redirect"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip("directory junctions are unavailable")

    with pytest.raises(ValueError, match="unsafe|reparse|link"):
        build_vault(junction / "new-vault")

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not (outside / "new-vault").exists()


def test_repeated_init_preserves_all_user_edited_protocol_files(tmp_path):
    paths = build_vault(tmp_path)
    files = (
        paths.system / "KNOWLEDGE_PROTOCOL.md",
        paths.root / "AGENTS.md",
        paths.root / "CLAUDE.md",
    )
    replacements = []
    for index, path in enumerate(files):
        content = f"使用者自訂內容 {index}\n".encode()
        path.write_bytes(content)
        replacements.append(content)

    build_vault(tmp_path)

    assert [path.read_bytes() for path in files] == replacements


def test_init_rejects_linked_or_hardlinked_protocol_target_without_writing_outside(
    tmp_path,
):
    paths = build_vault(tmp_path / "vault")
    target = paths.root / "AGENTS.md"
    target.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("不可改動\n", encoding="utf-8")
    try:
        os.link(outside, target)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(ValueError, match="template|link|unsafe"):
        build_vault(paths.root)

    assert outside.read_text(encoding="utf-8") == "不可改動\n"


def test_init_rejects_linked_system_directory_before_creating_files(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    system = root / "80_system"
    try:
        system.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(ValueError, match="unsafe|link|reparse"):
        build_vault(root)

    assert list(outside.iterdir()) == []


def test_windows_launcher_and_beginner_readme_explain_safe_daily_workflow():
    repository = Path(__file__).resolve().parents[1]
    launcher = (repository / "scripts" / "start-kb.ps1").read_text(encoding="utf-8")
    readme = (repository / "README.md").read_text(encoding="utf-8")

    assert '$ErrorActionPreference = "Stop"' in launcher
    assert 'param(' in launcher
    assert 'ValueFromRemainingArguments' not in launcher
    assert '".venv\\Scripts\\python.exe"' in launcher
    assert '-m local_kb.cli watch $Vault' in launcher
    assert "shell:startup" not in launcher.casefold()
    assert launcher.isascii(), "Windows PowerShell 5.1 must parse the launcher without a UTF-8 BOM"

    for required in (
        "初次安裝",
        "00_inbox",
        "kb prepare",
        "kb finalize",
        "kb lint",
        "git log",
        "git revert",
        "Codex",
        "Claude",
        "雲端模型",
        "裸網址",
        "pending_extractor",
        "shell:startup",
        "Codex Desktop",
        "Claude CLI",
    ):
        assert required in readme
    assert "不代表送給 AI 的內容仍然離線" in readme


def test_beginner_docs_use_ai_driven_installation_and_safe_excel_copy():
    repository = Path(__file__).resolve().parents[1]
    readme = (repository / "README.md").read_text(encoding="utf-8")
    guide = (
        repository / "docs" / "BEGINNER_GUIDE.zh-TW.md"
    ).read_text(encoding="utf-8")
    cli_reference = (
        repository / "docs" / "CLI_REFERENCE.zh-TW.md"
    ).read_text(encoding="utf-8")

    assert "用 Codex 首次安裝" in guide
    assert "用 Claude Code 首次安裝" in guide
    assert "不要叫我自己輸入" in guide
    assert "原始 Excel 永遠不能被搬走" in guide
    assert "只把 00_inbox 裡的副本交給 ingest-once" in guide
    assert "兩者都使用 `C:\\KnowledgeBase`" in readme
    assert "這份文件是給 AI 代理讀的" in cli_reference
    assert "一般使用者不需要輸入以下指令" in cli_reference
    assert "```powershell" not in readme.casefold()
    assert "```powershell" not in guide.casefold()


def test_python_module_entrypoint_invokes_cli_help():
    completed = subprocess.run(
        [sys.executable, "-m", "local_kb.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert "usage: kb" in completed.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher coverage")
def test_windows_launcher_stays_running_and_reports_bad_vault(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    launcher = repository / "scripts" / "start-kb.ps1"
    vault = build_vault(tmp_path / "vault with spaces 知識庫")
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(launcher), "-Vault", str(vault.root),
    ]
    if not (repository / ".venv" / "Scripts" / "python.exe").is_file():
        failed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert failed.returncode != 0
        assert "Missing .venv" in failed.stderr
        return

    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        time.sleep(1.0)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=10)

    failed = subprocess.run(
        command[:-1] + [str(tmp_path / "missing vault")],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert failed.returncode != 0
    assert "Vault directory not found" in failed.stderr


def test_source_distribution_manifest_includes_windows_launcher():
    repository = Path(__file__).resolve().parents[1]
    manifest = (repository / "MANIFEST.in").read_text(encoding="ascii")

    assert "include scripts/start-kb.ps1" in manifest


def test_compiler_factory_honors_provider_and_rejects_unknown(tmp_path):
    from local_kb.cli import _compiler_for_config
    from local_kb.compiler import ClaudeCompiler, ManualCompiler

    paths = build_vault(tmp_path)
    claude = _compiler_for_config(
        Config(vault=paths.root, compiler="claude"), paths
    )
    manual = _compiler_for_config(
        Config(vault=paths.root, compiler="manual"), paths
    )

    assert isinstance(claude, ClaudeCompiler)
    assert isinstance(claude.fallback, ManualCompiler)
    assert claude.cwd == paths.root
    assert isinstance(manual, ManualCompiler)
    with pytest.raises(ValueError, match="compiler.provider"):
        _compiler_for_config(Config(vault=paths.root, compiler="unknown"), paths)


def test_cli_claude_provider_publishes_compiler_receipt(tmp_path, monkeypatch):
    import local_kb.cli as cli_module

    paths = build_vault(tmp_path)
    source = paths.inbox / "source.md"
    source.write_text("選擇 B 方案。", encoding="utf-8")
    created = []

    class FakeClaude:
        def __init__(self, **kwargs):
            created.append(kwargs)

        def compile(self, evidence):
            source_id = evidence.split("source_id=", 1)[1].split(" ", 1)[0]
            return {"changes": [{
                "path": "20_wiki/work/decisions/b.md",
                "title": "B 方案",
                "type": "decision",
                "space": "work",
                "confidence": "high",
                "source_ids": [source_id],
                "current_state": "選擇 B 方案。",
                "conflicts": "",
                "timeline_entry": "已整理本地來源。",
            }]}

    monkeypatch.setattr(cli_module, "ClaudeCompiler", FakeClaude)

    assert cli_module.main([
        "ingest-once", str(paths.root), str(source), "--space", "work"
    ]) == 0
    assert (paths.wiki / "work" / "decisions" / "b.md").is_file()
    assert created[0]["cwd"] == paths.root


def test_cli_missing_claude_creates_actionable_handoff_without_false_success(
    tmp_path, monkeypatch
):
    import local_kb.compiler as compiler_module
    from local_kb.cli import main

    paths = build_vault(tmp_path)
    source = paths.inbox / "source.md"
    source.write_text("本地證據。", encoding="utf-8")

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("claude is unavailable")

    monkeypatch.setattr(compiler_module, "_run_bounded_process", unavailable)

    assert main([
        "ingest-once", str(paths.root), str(source), "--space", "work"
    ]) == 2
    jobs = DiskQueue(paths.queue).iter_jobs()
    assert len(jobs) == 1
    assert jobs[0].state == "pending_attention"
    assert jobs[0].metadata["compiler_status"] == "needs_agent"
    assert Path(paths.root / jobs[0].metadata["compiler_handoff"]).is_file()
    assert not list(paths.wiki.rglob("*.md"))


def test_finalize_then_watch_consumes_derived_job_without_raw_feedback(
    tmp_path, monkeypatch
):
    import local_kb.cli as cli_module
    from local_kb.watcher import StableTracker

    paths = build_vault(tmp_path)
    queue = DiskQueue(paths.queue)
    catalog = Catalog(paths.index / "catalog.sqlite3")
    incoming = paths.inbox / "proof.md"
    incoming.write_text("團隊選擇 B 方案。", encoding="utf-8")
    raw = IngestService(paths, queue, catalog).process(
        queue.enqueue(incoming).job_id, space="work"
    )
    packet = QueryService(catalog, vault=paths, queue=queue).prepare(
        "團隊選擇哪個方案？", {"work"}
    )
    evidence = next(item for item in packet["evidence"] if item["kind"] == "raw_fragment")
    answer = {
        "conclusion": "團隊選擇 B 方案。",
        "citations": [_citation(evidence)],
        "confidence": "high",
        "conflicts": "沒有衝突。",
    }
    result = finalize_and_enqueue(paths, queue, packet, answer)
    before = _source_count(catalog)

    class FakeClaude:
        def __init__(self, **kwargs):
            pass

        def compile(self, compiler_evidence):
            assert "evidence_kind=derived_answer" in compiler_evidence
            assert raw.source_id in compiler_evidence
            return {"changes": [{
                "path": "20_wiki/work/decisions/derived-b.md",
                "title": "B 方案結論",
                "type": "decision",
                "space": "work",
                "confidence": "high",
                "source_ids": [raw.source_id],
                "current_state": "團隊選擇 B 方案。",
                "conflicts": "",
                "timeline_entry": "由已引用答案整理。",
            }]}

    monkeypatch.setattr(cli_module, "ClaudeCompiler", FakeClaude)
    cli_module.watch_once(
        paths, StableTracker(0, trusted_root=paths.inbox), set(), space="work"
    )

    derived_job = queue.get(result.job_id)
    assert derived_job.state == "published"
    assert derived_job.metadata["compiler_status"] == "completed"
    wiki = paths.wiki / "work" / "decisions" / "derived-b.md"
    assert raw.source_id in wiki.read_text(encoding="utf-8")
    assert _source_count(catalog) == before


def test_finalize_then_manual_watch_is_actionable_and_not_left_discovered(tmp_path):
    from local_kb.watcher import StableTracker

    paths = build_vault(tmp_path)
    paths.config.write_text(
        paths.config.read_text(encoding="utf-8").replace(
            'provider = "claude"', 'provider = "manual"'
        ),
        encoding="utf-8",
    )
    queue = DiskQueue(paths.queue)
    catalog = Catalog(paths.index / "catalog.sqlite3")
    incoming = paths.inbox / "proof.md"
    incoming.write_text("團隊選擇 B 方案。", encoding="utf-8")
    raw = IngestService(paths, queue, catalog).process(
        queue.enqueue(incoming).job_id, space="work"
    )
    packet = QueryService(catalog, vault=paths, queue=queue).prepare(
        "選擇哪個方案？", {"work"}
    )
    evidence = next(item for item in packet["evidence"] if item["kind"] == "raw_fragment")
    result = finalize_and_enqueue(paths, queue, packet, {
        "conclusion": "選擇 B 方案。",
        "citations": [_citation(evidence)],
        "confidence": "high",
        "conflicts": "",
    })
    before = _source_count(catalog)

    from local_kb.cli import watch_once
    watch_once(paths, StableTracker(0, trusted_root=paths.inbox), set(), space="work")

    job = queue.get(result.job_id)
    assert job.state == "pending_attention"
    assert job.metadata["compiler_status"] == "needs_agent"
    assert Path(paths.root / job.metadata["compiler_handoff"]).is_file()
    assert _source_count(catalog) == before

    class ReplacementCompiler:
        def compile(self, compiler_evidence):
            return {"changes": [{
                "path": "20_wiki/work/decisions/resumed-b.md",
                "title": "恢復後的 B 方案",
                "type": "decision",
                "space": "work",
                "confidence": "high",
                "source_ids": [raw.source_id],
                "current_state": "選擇 B 方案。",
                "conflicts": "",
                "timeline_entry": "人工工作已安全恢復。",
            }]}

    service = IngestService(paths, queue, catalog)
    service.resume_derived_update(result.job_id, compiler=ReplacementCompiler())

    assert queue.get(result.job_id).state == "published"
    assert (paths.wiki / "work" / "decisions" / "resumed-b.md").is_file()
    assert _source_count(catalog) == before
