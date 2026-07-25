import os
import re
import tomllib
from pathlib import Path

import pytest

from local_kb.cli import build_vault, main
from local_kb.config import Config
from local_kb.paths import VaultPaths


VALID_CONFIG = """[compiler]
provider = "claude"

[watcher]
poll_seconds = 2.0
stable_seconds = 5.0

[queue]
max_retries = 3
"""


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


def test_build_vault_preserves_existing_config_bytes(tmp_path):
    build_vault(tmp_path)
    config_path = tmp_path / "80_system" / "config.toml"
    custom_config = b"""# Keep this customized configuration byte-for-byte.
[compiler]
provider = "codex"

[watcher]
poll_seconds = 3
stable_seconds = 0

[queue]
max_retries = 7
"""
    config_path.write_bytes(custom_config)

    build_vault(tmp_path)

    assert config_path.read_bytes() == custom_config
    assert Config.load(config_path) == Config(
        vault=tmp_path.resolve(),
        compiler="codex",
        poll_seconds=3.0,
        stable_seconds=0.0,
        max_retries=7,
    )
    assert not list(config_path.parent.glob("*.tmp"))


def test_build_vault_preserves_concurrent_config_and_cleans_temp(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "80_system" / "config.toml"
    concurrent_config = VALID_CONFIG.replace('"claude"', '"codex"').encode()

    def publish_concurrent_config(source, destination):
        assert Path(source).parent == config_path.parent
        Path(destination).write_bytes(concurrent_config)
        raise FileExistsError

    monkeypatch.setattr(os, "link", publish_concurrent_config)

    build_vault(tmp_path)

    assert config_path.read_bytes() == concurrent_config
    assert not list(config_path.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ('provider = "claude"\n', "", "compiler.provider"),
        ('provider = "claude"', 'provider = " "', "compiler.provider"),
        (
            "[watcher]\npoll_seconds = 2.0\nstable_seconds = 5.0\n\n",
            "",
            "watcher.poll_seconds",
        ),
        ("poll_seconds = 2.0", 'poll_seconds = "2.0"', "watcher.poll_seconds"),
        ("poll_seconds = 2.0", "poll_seconds = true", "watcher.poll_seconds"),
        ("poll_seconds = 2.0", "poll_seconds = 0", "watcher.poll_seconds"),
        ("poll_seconds = 2.0", "poll_seconds = nan", "watcher.poll_seconds"),
        ("poll_seconds = 2.0", "poll_seconds = inf", "watcher.poll_seconds"),
        ("poll_seconds = 2.0", "poll_seconds = -inf", "watcher.poll_seconds"),
        ("stable_seconds = 5.0\n", "", "watcher.stable_seconds"),
        ("stable_seconds = 5.0", "stable_seconds = true", "watcher.stable_seconds"),
        ("stable_seconds = 5.0", "stable_seconds = -1", "watcher.stable_seconds"),
        ("stable_seconds = 5.0", "stable_seconds = nan", "watcher.stable_seconds"),
        ("stable_seconds = 5.0", "stable_seconds = inf", "watcher.stable_seconds"),
        ("stable_seconds = 5.0", "stable_seconds = -inf", "watcher.stable_seconds"),
        ("[queue]\nmax_retries = 3\n", "", "queue.max_retries"),
        ("max_retries = 3", "max_retries = 0", "queue.max_retries"),
        ("max_retries = 3", "max_retries = true", "queue.max_retries"),
        ("max_retries = 3", "max_retries = 3.0", "queue.max_retries"),
    ],
)
def test_config_load_rejects_invalid_required_fields(tmp_path, old, new, field):
    config_path = tmp_path / "80_system" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(VALID_CONFIG.replace(old, new), encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape(field)):
        Config.load(config_path)


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
