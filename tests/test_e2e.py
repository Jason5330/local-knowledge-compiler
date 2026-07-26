import json
import os
from pathlib import Path

import pytest

from local_kb.catalog import Catalog
from local_kb.cli import build_vault
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


def test_offline_ingest_prepare_cited_answer_finalize_and_derived_job(tmp_path):
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
    ):
        assert required in protocol
    assert "80_system/KNOWLEDGE_PROTOCOL.md" in agents
    assert "80_system/KNOWLEDGE_PROTOCOL.md" in claude
    assert "kb prepare" not in agents
    assert "kb prepare" not in claude


def test_fresh_init_is_immediately_healthy(tmp_path):
    paths = build_vault(tmp_path)

    report = lint(paths)

    assert report["healthy"] is True
    assert report["issues"]["index_raw_mismatches"] == []


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
