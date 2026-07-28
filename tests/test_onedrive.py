from io import StringIO
import json

from local_kb.cli import build_vault, main
from local_kb.onedrive import (
    ONEDRIVE_WARNING,
    find_onedrive_root,
    warn_if_onedrive,
)


def test_personal_onedrive_child_is_detected(tmp_path):
    root = tmp_path / "OneDrive"
    vault = root / "project" / "KnowledgeBase"
    environment = {"OneDrive": str(root)}

    assert find_onedrive_root(vault, environment) == root.resolve()


def test_business_onedrive_child_is_detected(tmp_path):
    root = tmp_path / "OneDrive - Example Company"
    vault = root / "project" / "KnowledgeBase"
    environment = {"OneDriveCommercial": str(root)}

    assert find_onedrive_root(vault, environment) == root.resolve()


def test_similar_prefix_outside_root_is_not_detected(tmp_path):
    root = tmp_path / "OneDrive"
    vault = tmp_path / "OneDrive-Backup" / "KnowledgeBase"

    assert find_onedrive_root(
        vault,
        {"OneDrive": str(root)},
    ) is None


def test_empty_or_invalid_environment_values_are_ignored(tmp_path):
    assert find_onedrive_root(
        tmp_path / "KnowledgeBase",
        {
            "OneDrive": "",
            "OneDriveConsumer": "\x00",
        },
    ) is None


def test_warning_is_written_only_to_supplied_stream(tmp_path):
    root = tmp_path / "OneDrive"
    stream = StringIO()

    matched = warn_if_onedrive(
        root / "KnowledgeBase",
        environ={"OneDrive": str(root)},
        stream=stream,
    )

    assert matched is True
    assert stream.getvalue() == ONEDRIVE_WARNING + "\n"


def test_non_onedrive_path_emits_nothing(tmp_path):
    stream = StringIO()

    matched = warn_if_onedrive(
        tmp_path / "KnowledgeBase",
        environ={},
        stream=stream,
    )

    assert matched is False
    assert stream.getvalue() == ""


def test_status_warning_does_not_corrupt_json_or_exit_code(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "OneDrive"
    project = root / "project"
    project.mkdir(parents=True)
    build_vault(project / "KnowledgeBase")
    monkeypatch.setenv("OneDrive", str(root))
    monkeypatch.chdir(project)

    result = main(["status"])

    captured = capsys.readouterr()
    assert result == 0
    assert json.loads(captured.out)["healthy"] is True
    assert captured.err == ONEDRIVE_WARNING + "\n"


def test_init_warning_continues_and_creates_vault(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "OneDrive"
    project = root / "project"
    project.mkdir(parents=True)
    monkeypatch.setenv("OneDriveConsumer", str(root))
    monkeypatch.chdir(project)

    result = main(["init"])

    captured = capsys.readouterr()
    assert result == 0
    assert (project / "KnowledgeBase").is_dir()
    assert captured.err == ONEDRIVE_WARNING + "\n"
