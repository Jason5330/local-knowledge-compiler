import json

from local_kb.cli import build_vault, main
from local_kb.correction_index import CorrectionIndex
from local_kb.correction_store import CorrectionStore
from test_correction_model import _record
from test_correction_service import _packet, _proposal


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_correct_cli_creates_active_correction(tmp_path, capsys):
    paths = build_vault(tmp_path / "KnowledgeBase")
    packet = _packet()
    packet_path = tmp_path / "packet.json"
    proposal_path = tmp_path / "proposal.json"
    _write_json(packet_path, packet)
    _write_json(proposal_path, _proposal(packet))

    result = main([
        "correct",
        "--vault",
        str(paths.root),
        "--packet",
        str(packet_path),
        "--proposal",
        str(proposal_path),
    ])

    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "active"
    assert report["created"] is True


def test_correct_cli_uses_project_local_vault(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paths = build_vault(tmp_path / "KnowledgeBase")
    packet = _packet()
    packet_path = tmp_path / "packet.json"
    proposal_path = tmp_path / "proposal.json"
    _write_json(packet_path, packet)
    _write_json(proposal_path, _proposal(packet))

    assert main([
        "correct",
        "--packet",
        str(packet_path),
        "--proposal",
        str(proposal_path),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "active"
    assert any(paths.correction_records.iterdir())


def test_correct_cli_rejects_malformed_proposal(tmp_path, capsys):
    paths = build_vault(tmp_path / "KnowledgeBase")
    packet_path = tmp_path / "packet.json"
    proposal_path = tmp_path / "proposal.json"
    _write_json(packet_path, _packet())
    proposal_path.write_text("[]", encoding="utf-8")

    assert main([
        "correct",
        "--vault",
        str(paths.root),
        "--packet",
        str(packet_path),
        "--proposal",
        str(proposal_path),
    ]) == 1
    assert "kb:" in capsys.readouterr().err


def test_correction_management_commands_list_show_and_suspend(
    tmp_path,
    capsys,
):
    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    record = store.create(_record())
    CorrectionIndex(paths).rebuild(store)

    assert main([
        "corrections-list",
        "--vault",
        str(paths.root),
        "--status",
        "active",
    ]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["records"][0]["correction_id"] == record.correction_id

    assert main([
        "corrections-show",
        "--vault",
        str(paths.root),
        "--correction-id",
        record.correction_id,
    ]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["record"]["content_sha256"] == record.content_sha256

    assert main([
        "corrections-set-status",
        "--vault",
        str(paths.root),
        "--correction-id",
        record.correction_id,
        "--status",
        "suspended",
        "--reason",
        "使用者要求暫停",
        "--expected-hash",
        record.content_sha256,
    ]) == 0
    changed = json.loads(capsys.readouterr().out)
    assert changed["status"] == "suspended"


def test_corrections_check_reports_index_health(tmp_path, capsys):
    paths = build_vault(tmp_path / "KnowledgeBase")
    store = CorrectionStore(paths)
    store.create(_record())
    CorrectionIndex(paths).rebuild(store)

    assert main([
        "corrections-check",
        "--vault",
        str(paths.root),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["healthy"] is True
