from local_kb.cli import build_vault, main
from local_kb.config import Config
from local_kb.paths import VaultPaths
import tomllib


def test_build_vault_creates_required_layout(tmp_path):
    build_vault(tmp_path)

    for directory in (
        "00_inbox",
        "10_raw",
        "20_wiki",
        "30_answers",
        "40_index",
        "80_system",
        "90_logs",
        "99_trash",
        ".kb",
        ".kb/queue",
    ):
        assert (tmp_path / directory).is_dir()

    assert (tmp_path / "80_system" / "config.toml").is_file()


def test_build_vault_creates_source_categories_and_default_config(tmp_path):
    build_vault(tmp_path)

    for root in ("10_raw", "20_wiki"):
        for category in ("personal", "work", "projects", "shared", "unclassified"):
            assert (tmp_path / root / category).is_dir()

    config = Config.load(tmp_path / "80_system" / "config.toml")
    assert config == Config(vault=tmp_path.resolve())
    with (tmp_path / "80_system" / "config.toml").open("rb") as config_file:
        assert tomllib.load(config_file)["compiler"]["provider"] == "claude"


def test_vault_paths_exposes_approved_roots(tmp_path):
    paths = VaultPaths(tmp_path)

    assert paths.inbox == tmp_path / "00_inbox"
    assert paths.raw == tmp_path / "10_raw"
    assert paths.wiki == tmp_path / "20_wiki"
    assert paths.answers == tmp_path / "30_answers"
    assert paths.index == tmp_path / "40_index"
    assert paths.system == tmp_path / "80_system"
    assert paths.logs == tmp_path / "90_logs"
    assert paths.trash == tmp_path / "99_trash"
    assert paths.runtime == tmp_path / ".kb"
    assert paths.queue == paths.runtime / "queue"
    assert paths.staging == paths.runtime / "staging"
    assert not hasattr(paths, "kb")
    assert not hasattr(paths, "state")


def test_main_initializes_requested_vault_and_reports_absolute_path(tmp_path, capsys):
    exit_code = main(["init", str(tmp_path)])

    assert exit_code == 0
    assert capsys.readouterr().out == f"Initialized knowledge vault: {tmp_path.resolve()}\n"
