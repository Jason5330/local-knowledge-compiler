"""Command-line interface for local knowledge vaults."""

import argparse
import os
from pathlib import Path
import tempfile
import time
from typing import Sequence

from .catalog import Catalog
from .config import Config
from .finalize import finalize_and_enqueue, read_json_document
from .health import lint, rebuild_catalog
from .ingest import IngestService
from .paths import VaultPaths
from .queue import DiskQueue, WriterLock
from .query import QueryService, write_packet
from .search import ranked_search
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
    finalize_parser = subcommands.add_parser("finalize", help="save a cited derived answer")
    finalize_parser.add_argument("--vault", type=Path, default=Path.cwd())
    finalize_parser.add_argument("--packet", type=Path, required=True)
    finalize_parser.add_argument("--answer", type=Path, required=True)
    lint_parser = subcommands.add_parser("lint", help="inspect vault health without changing it")
    lint_parser.add_argument("--vault", type=Path, default=Path.cwd())
    rebuild_parser = subcommands.add_parser("rebuild", help="rebuild the search catalog from cache")
    rebuild_parser.add_argument("--vault", type=Path, default=Path.cwd())
    arguments = parser.parse_args(argv)

    if arguments.command == "init":
        paths = build_vault(arguments.path)
        print(f"Initialized knowledge vault: {paths.root}")
        return 0

    paths = VaultPaths(arguments.vault.resolve())
    try:
        if arguments.command == "lint":
            import json

            report = lint(paths)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0 if report.get("healthy") is True else 2
        if arguments.command == "rebuild":
            print(f"Indexed sources: {rebuild_catalog(paths)}")
            return 0
        config = Config.load(paths.config)
        if arguments.command == "prepare":
            catalog = Catalog(paths.index / "catalog.sqlite3")
            spaces, selection = _select_prepare_spaces(catalog, arguments.question, arguments.space)
            queue = (
                DiskQueue(paths.queue, config.max_retries)
                if _queue_has_job(paths.queue)
                else None
            )
            packet = QueryService(catalog, vault=paths, queue=queue).prepare(
                arguments.question, spaces, space_selection=selection
            )
            print(write_packet(paths, packet, arguments.output))
            return 0
        if arguments.command == "finalize":
            with WriterLock(paths.runtime / "write.lock", timeout=0):
                queue = DiskQueue(paths.queue, config.max_retries)
                result = finalize_and_enqueue(
                    paths,
                    queue,
                    read_json_document(arguments.packet),
                    read_json_document(arguments.answer),
                )
            print(f"Saved answer: {result.path}")
            print(f"Queued derived update: {result.job_id}")
            return 0
        if arguments.command == "ingest-once":
            with WriterLock(paths.runtime / "write.lock", timeout=0):
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


def _queue_has_job(path: Path, *, max_entries: int = 10_000) -> bool:
    if not path.is_dir():
        return False
    with os.scandir(path) as entries:
        for index, entry in enumerate(entries):
            if index >= max_entries:
                return False
            if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                return True
    return False


def _select_prepare_spaces(catalog: Catalog, question: str, explicit: list[str]) -> tuple[list[str], str]:
    if explicit:
        return explicit, "explicit"
    lowered = question.casefold()
    if any(marker in question for marker in ("個人", "私密")) or "personal" in lowered:
        return ["personal"], "inferred_personal"
    if any(marker in question for marker in ("工作",)) or "work" in lowered:
        return ["work"], "inferred_work"
    import re
    match = re.search(r"project:([a-z0-9]+(?:-[a-z0-9]+)*)", lowered)
    if match:
        return [f"project:{match.group(1)}"], "inferred_project"
    candidates = ["work", "shared", "unclassified"]
    hits = {space: ranked_search(catalog, question, {space}, limit=1) for space in candidates}
    matched = [space for space, found in hits.items() if found]
    if len(matched) == 1:
        return matched, "inferred_preview"
    raise ValueError("cannot infer a safe space; pass --space explicitly")


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
    with WriterLock(paths.runtime / "write.lock", timeout=0):
        return _watch_once_locked(paths, tracker, submitted, space=space)


def _watch_once_locked(
    paths: VaultPaths, tracker: StableTracker, submitted: set[Path], *, space: str
) -> list:
    config = Config.load(paths.config)
    queue = DiskQueue(paths.queue, config.max_retries)
    service = IngestService(paths, queue, Catalog(paths.index / "catalog.sqlite3"))
    results = []
    for job in queue.iter_jobs():
        if job.metadata.get("job_type") == "derived_update":
            continue
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
