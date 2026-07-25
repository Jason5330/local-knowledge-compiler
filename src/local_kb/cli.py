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
from .query import QueryService, write_packet
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
    prepare_parser = subcommands.add_parser("prepare", help="prepare a grounded local evidence packet")
    prepare_parser.add_argument("question")
    prepare_parser.add_argument("--vault", type=Path, default=Path.cwd())
    prepare_parser.add_argument("--space", action="append", default=[])
    prepare_parser.add_argument("--output", type=Path, default=Path(".kb/last-packet.json"))
    arguments = parser.parse_args(argv)

    if arguments.command == "init":
        paths = build_vault(arguments.path)
        print(f"Initialized knowledge vault: {paths.root}")
        return 0

    paths = VaultPaths(arguments.vault.resolve())
    try:
        config = Config.load(paths.config)
        if arguments.command == "prepare":
            catalog = Catalog(paths.index / "catalog.sqlite3")
            catalog.initialize()
            spaces = arguments.space or ["unclassified"]
            packet = QueryService(catalog, vault=paths, queue=DiskQueue(paths.queue, config.max_retries)).prepare(
                arguments.question, spaces
            )
            print(write_packet(paths, packet, arguments.output))
            return 0
        if arguments.command == "ingest-once":
            queue = DiskQueue(paths.queue, config.max_retries)
            service = IngestService(paths, queue, Catalog(paths.index / "catalog.sqlite3"))
            source_path = arguments.path.resolve(strict=True)
            service._hash_pinned_regular(source_path)
            job = queue.enqueue(source_path)
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
    for job in queue.iter_jobs():
        if job.state == "pending_attention":
            original = Path(str(job.metadata.get("original_source_path", job.source_path)))
            submitted.discard(original)
            continue
        if job.state == "published":
            continue
        try:
            result = service.process(job.job_id, space=str(job.metadata.get("space", space)))
        except Exception as error:
            print(f"kb watch: {job.job_id}: {error}", file=__import__("sys").stderr)
            continue
        results.append(result)
        original = Path(str(job.metadata.get("original_source_path", job.source_path)))
        submitted.discard(original)
        tracker.forget(original)
    for candidate in sorted(tracker.iter_trusted_children()):
        if candidate in submitted or not tracker.observe(candidate):
            continue
        candidate = candidate.resolve(strict=True)
        active = queue.active_for_source(candidate)
        if active is not None:
            continue
        job = queue.enqueue(candidate)
        submitted.add(candidate)
        try:
            results.append(service.process(job.job_id, space=space))
        except Exception as error:
            print(f"kb watch: {job.job_id}: {error}", file=__import__("sys").stderr)
            continue
        submitted.discard(candidate)
        tracker.forget(candidate)
    tracker.prune()
    return results
