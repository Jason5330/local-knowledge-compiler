"""Command-line interface for local knowledge vaults."""

import argparse
from pathlib import Path
from typing import Sequence

from .paths import VaultPaths


ROOTS = (
    "inbox",
    "raw",
    "wiki",
    "answers",
    "index",
    "system",
    "logs",
    "trash",
    "runtime",
)
CATEGORIES = ("personal", "work", "projects", "shared", "unclassified")
DEFAULT_CONFIG = """[compiler]
provider = \"claude\"

[watcher]
poll_seconds = 2.0
stable_seconds = 5.0

[queue]
max_retries = 3
"""


def build_vault(root: Path) -> VaultPaths:
    """Create the standard layout and default configuration for a vault."""
    paths = VaultPaths(Path(root).resolve())
    for name in ROOTS:
        getattr(paths, name).mkdir(parents=True, exist_ok=True)
    for source_root in (paths.raw, paths.wiki):
        for category in CATEGORIES:
            (source_root / category).mkdir(exist_ok=True)
    paths.queue.mkdir(parents=True, exist_ok=True)
    paths.staging.mkdir(parents=True, exist_ok=True)
    try:
        with paths.config.open("x", encoding="utf-8") as config_file:
            config_file.write(DEFAULT_CONFIG)
    except FileExistsError:
        pass
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kb")
    subcommands = parser.add_subparsers(dest="command", required=True)
    init_parser = subcommands.add_parser("init", help="initialize a knowledge vault")
    init_parser.add_argument("path", type=Path)
    arguments = parser.parse_args(argv)

    if arguments.command == "init":
        paths = build_vault(arguments.path)
        print(f"Initialized knowledge vault: {paths.root}")
        return 0

    return 1
