import json

from local_kb.cli import build_vault, main
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
