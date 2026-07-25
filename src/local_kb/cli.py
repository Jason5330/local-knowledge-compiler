"""Command-line interface for local knowledge vaults."""

import argparse
import os
from pathlib import Path
import tempfile
import time
from typing import Sequence

from .catalog import Catalog
from .config import Config
from .ingest import IngestService
from .paths import VaultPaths
from .queue import DiskQueue
from .watcher import StableTracker


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
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=paths.system,
            prefix=f".{paths.config.name}.",
            suffix=".tmp",
            delete=False,
        ) as config_file:
            temporary_path = Path(config_file.name)
            config_file.write(DEFAULT_CONFIG)
            config_file.flush()
            os.fsync(config_file.fileno())
        try:
            os.link(temporary_path, paths.config)
        except FileExistsError:
            pass
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kb")
    subcommands = parser.add_subparsers(dest="command", required=True)
    init_parser = subcommands.add_parser("init", help="initialize a knowledge vault")
    init_parser.add_argument("path", type=Path)
    watch_parser = subcommands.add_parser("watch", help="ingest stable direct inbox files")
    watch_parser.add_argument("vault", type=Path)
    watch_parser.add_argument("--space", default="unclassified")
    once_parser = subcommands.add_parser("ingest-once", help="ingest one file")
    once_parser.add_argument("vault", type=Path)
    once_parser.add_argument("path", type=Path)
    once_parser.add_argument("--space", default="unclassified")
    arguments = parser.parse_args(argv)

    if arguments.command == "init":
        paths = build_vault(arguments.path)
        print(f"Initialized knowledge vault: {paths.root}")
        return 0

    paths = VaultPaths(arguments.vault.resolve())
    try:
        config = Config.load(paths.config)
        if arguments.command == "ingest-once":
            queue = DiskQueue(paths.queue, config.max_retries)
            service = IngestService(paths, queue, Catalog(paths.index / "catalog.sqlite3"))
            job = queue.enqueue(arguments.path)
            source = service.process(job.job_id, space=arguments.space)
            print(f"{source.version_id} {source.status}")
            return 0
        tracker = StableTracker(config.stable_seconds, trusted_root=paths.inbox)
        submitted: set[Path] = set()
        while True:
            for source in watch_once(paths, tracker, submitted, space=arguments.space):
                print(f"{source.version_id} {source.status}")
            time.sleep(config.poll_seconds)
    except Exception as error:
        print(f"kb: {error}", file=__import__("sys").stderr)
        return 1

    return 1


def watch_once(
    vault: VaultPaths | Path | str,
    tracker: StableTracker,
    submitted: set[Path],
    *,
    space: str = "unclassified",
) -> list:
    """Run one poll iteration; kept separate so CLI polling is testable."""
    paths = vault if isinstance(vault, VaultPaths) else VaultPaths(Path(vault).resolve())
    if tracker.trusted_root is None:
        tracker.bind_trusted_root(paths.inbox)
    elif tracker.trusted_root != Path(os.path.abspath(os.fspath(paths.inbox))):
        raise ValueError("watch tracker is bound to a different inbox")
    config = Config.load(paths.config)
    queue = DiskQueue(paths.queue, config.max_retries)
    service = IngestService(paths, queue, Catalog(paths.index / "catalog.sqlite3"))
    results = []
    for candidate in sorted(tracker.iter_trusted_children()):
        if candidate in submitted or not tracker.observe(candidate):
            continue
        job = queue.enqueue(candidate)
        submitted.add(candidate)
        results.append(service.process(job.job_id, space=space))
    return results
