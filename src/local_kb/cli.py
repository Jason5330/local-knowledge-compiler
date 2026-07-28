"""Command-line interface for local knowledge vaults."""

import argparse
from importlib import resources
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from typing import Sequence
from uuid import uuid4

from .catalog import Catalog
from .compiler import ClaudeCompiler, ManualCompiler
from .config import Config
from .finalize import finalize_and_enqueue, read_json_document
from .health import lint, rebuild_catalog
from .ingest import IngestService
from .onedrive import warn_if_onedrive
from .paths import VaultPaths
from .project import default_vault_path, resolve_vault_path
from .project_setup import configure_git_protection
from .queue import DiskQueue, WriterLock
from .query import QueryService, write_packet
from .search import ranked_search
from .safety import secure_directory, verify_catalog_paths
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
STATUS_MAX_JOBS = 10_000
STATUS_MAX_BYTES = 16 * 1024 * 1024
STATUS_MAX_ENTRIES = 20_000


def _compiler_for_config(config: Config, paths: VaultPaths):
    fallback = ManualCompiler(
        paths.runtime / "manual", trusted_root=paths.root
    )
    provider = config.compiler.strip().casefold()
    if provider == "manual":
        return fallback
    if provider == "claude":
        return ClaudeCompiler(fallback=fallback, cwd=paths.root)
    raise ValueError(
        "compiler.provider must be either 'claude' or 'manual'"
    )


def build_vault(root: Path) -> VaultPaths:
    """Create the standard layout and default configuration for a vault."""
    requested_root = secure_directory(Path(root).absolute())
    paths = VaultPaths(requested_root)
    secure_directory(paths.runtime)
    with WriterLock(paths.runtime / "init.lock", timeout=30):
        for name in ROOTS:
            _ensure_vault_directory(paths.root, getattr(paths, name))
        for source_root in (paths.raw, paths.wiki):
            for category in CATEGORIES:
                _ensure_vault_directory(paths.root, source_root / category)
        _ensure_vault_directory(paths.root, paths.queue)
        _ensure_vault_directory(paths.root, paths.staging)
        _install_once(
            paths.root,
            paths.config,
            DEFAULT_CONFIG.encode("utf-8"),
            publish_with_link=True,
        )
        catalog_path = paths.index / "catalog.sqlite3"
        verify_catalog_paths(catalog_path, allow_missing_main=True)
        with tempfile.TemporaryDirectory(prefix="local-kb-catalog-") as catalog_stage:
            staged_catalog = Path(catalog_stage) / "catalog.sqlite3"
            Catalog(staged_catalog).initialize()
            _install_once(paths.root, catalog_path, staged_catalog.read_bytes())
        verify_catalog_paths(catalog_path)
        _validate_initialized_catalog(catalog_path)
        for template_name, destination in (
            ("KNOWLEDGE_PROTOCOL.md", paths.system / "KNOWLEDGE_PROTOCOL.md"),
            ("AGENTS.md", paths.root / "AGENTS.md"),
            ("CLAUDE.md", paths.root / "CLAUDE.md"),
        ):
            payload = (
                resources.files("local_kb")
                .joinpath("templates", template_name)
                .read_bytes()
            )
            _install_once(paths.root, destination, payload)
    return paths


def _validate_initialized_catalog(path: Path) -> None:
    import sqlite3

    try:
        with Catalog(path).connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            quick = connection.execute("PRAGMA quick_check").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type IN ('table', 'view')"
                )
            }
        required = {"sources", "source_fragments", "source_fts", "source_fts_map"}
        if version != Catalog.SCHEMA_VERSION or quick != "ok" or not required <= tables:
            raise ValueError
    except (OSError, ValueError, sqlite3.Error) as error:
        raise ValueError("catalog is invalid; run kb rebuild") from error


def _is_reparse_path(path: Path) -> bool:
    try:
        return bool(os.lstat(path).st_file_attributes & 0x400)
    except AttributeError:
        return False
    except OSError:
        return True


def _ensure_vault_directory(root: Path, directory: Path) -> None:
    """Create and pin one directory without traversing links or junctions."""
    from .compiler import ManualCompiler

    try:
        directory.relative_to(root)
    except ValueError as error:
        raise ValueError("vault directory must stay inside the vault") from error
    with ManualCompiler(directory, trusted_root=root)._pinned_outbox():
        pass


def _safe_installed_file(
    destination: Path, *, directory_fd: int | None
) -> os.stat_result | None:
    try:
        info = (
            os.stat(destination.name, dir_fd=directory_fd, follow_symlinks=False)
            if directory_fd is not None
            else os.lstat(destination)
        )
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse_path(destination)
        or info.st_nlink != 1
    ):
        raise ValueError(f"managed target is an unsafe link: {destination.name}")
    return info


def _install_once(
    root: Path,
    destination: Path,
    payload: bytes,
    *,
    publish_with_link: bool = False,
) -> None:
    """Publish a default file once; never replace user-owned bytes."""
    from .compiler import ManualCompiler

    if not isinstance(payload, bytes) or len(payload) > 1_000_000:
        raise ValueError("template payload is invalid")
    locker = ManualCompiler(destination.parent, trusted_root=root)
    with locker._pinned_outbox(create=False) as directory_fd:
        if _safe_installed_file(destination, directory_fd=directory_fd) is not None:
            return
        temporary_name = f".{destination.name}.{uuid4().hex}.tmp"
        temporary_path = destination.parent / temporary_name
        descriptor: int | None = None
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = (
                os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
                if directory_fd is not None
                else os.open(temporary_path, flags, 0o600)
            )
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                if directory_fd is not None:
                    os.link(
                        temporary_name,
                        destination.name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    os.fsync(directory_fd)
                elif publish_with_link:
                    os.link(temporary_path, destination)
                else:
                    # Windows rename is an atomic no-replace operation.  It also
                    # avoids exposing a partially written destination.
                    os.rename(temporary_path, destination)
            except FileExistsError:
                _safe_installed_file(destination, directory_fd=directory_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                if directory_fd is not None:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                else:
                    temporary_path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
        _safe_installed_file(destination, directory_fd=directory_fd)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kb")
    subcommands = parser.add_subparsers(dest="command", required=True)
    init_parser = subcommands.add_parser("init", help="initialize a knowledge vault")
    init_parser.add_argument("path", type=Path, nargs="?")
    watch_parser = subcommands.add_parser("watch", help="ingest stable direct inbox files")
    watch_parser.add_argument("vault", type=Path)
    watch_parser.add_argument("--space", default="unclassified")
    once_parser = subcommands.add_parser("ingest-once", help="ingest one file")
    once_parser.add_argument("vault", type=Path)
    once_parser.add_argument("path", type=Path)
    once_parser.add_argument("--space", default="unclassified")
    prepare_parser = subcommands.add_parser("prepare", help="prepare a grounded local evidence packet")
    prepare_parser.add_argument("question")
    prepare_parser.add_argument("--vault", type=Path)
    prepare_parser.add_argument("--space", action="append", default=[])
    prepare_parser.add_argument("--output", type=Path, default=Path(".kb/last-packet.json"))
    finalize_parser = subcommands.add_parser("finalize", help="save a cited derived answer")
    finalize_parser.add_argument("--vault", type=Path)
    finalize_parser.add_argument("--packet", type=Path, required=True)
    finalize_parser.add_argument("--answer", type=Path, required=True)
    status_parser = subcommands.add_parser(
        "status", help="list actionable queue jobs without changing the vault"
    )
    status_parser.add_argument("--vault", type=Path)
    resume_parser = subcommands.add_parser(
        "resume", help="resume one actionable compiler handoff"
    )
    resume_parser.add_argument("--vault", type=Path)
    resume_parser.add_argument("--job-id", required=True)
    lint_parser = subcommands.add_parser("lint", help="inspect vault health without changing it")
    lint_parser.add_argument("--vault", type=Path)
    rebuild_parser = subcommands.add_parser("rebuild", help="rebuild the search catalog from cache")
    rebuild_parser.add_argument("--vault", type=Path)
    arguments = parser.parse_args(argv)

    if arguments.command == "init":
        project_local = arguments.path is None
        target = (
            default_vault_path()
            if project_local
            else arguments.path
        )
        try:
            paths = build_vault(target)
            warn_if_onedrive(paths.root)
        except Exception as error:
            print(f"kb: {error}", file=sys.stderr)
            return 1
        if project_local:
            try:
                configure_git_protection(Path.cwd(), paths.root)
            except RuntimeError as error:
                print(f"資料保護提醒：{error}", file=sys.stderr)
                print(f"Initialized knowledge vault: {paths.root}")
                return 2
        print(f"Initialized knowledge vault: {paths.root}")
        return 0

    try:
        paths = _paths_for_arguments(arguments)
        warn_if_onedrive(paths.root)
        if arguments.command == "status":
            report = _status_report(paths)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 2 if report["attention_required"] else 0
        if arguments.command == "lint":
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
        if arguments.command == "resume":
            with WriterLock(paths.runtime / "write.lock", timeout=0):
                queue = DiskQueue(paths.queue, config.max_retries)
                service = IngestService(
                    paths,
                    queue,
                    Catalog(paths.index / "catalog.sqlite3"),
                    compiler=_compiler_for_config(config, paths),
                )
                job = queue.get(arguments.job_id)
                try:
                    if job.metadata.get("job_type") == "derived_update":
                        service.resume_derived_update(job.job_id)
                    else:
                        service.resume_compilation(job.job_id)
                except Exception as error:
                    try:
                        current = queue.get(job.job_id)
                        if current.state != "published":
                            queue.fail(job.job_id, error)
                    except Exception:
                        pass
                    raise
                current = queue.get(job.job_id)
            if current.state == "published":
                print(f"Job {current.job_id}: published")
                return 0
            if current.state == "pending_attention":
                _print_pending_job(current, config.compiler)
                return 2
            print(f"Job {current.job_id}: {current.state}")
            return 2
        if arguments.command == "ingest-once":
            with WriterLock(paths.runtime / "write.lock", timeout=0):
                queue = DiskQueue(paths.queue, config.max_retries)
                service = IngestService(
                    paths,
                    queue,
                    Catalog(paths.index / "catalog.sqlite3"),
                    compiler=_compiler_for_config(config, paths),
                )
                source_path = arguments.path.resolve(strict=True)
                service._hash_pinned_regular(source_path)
                job = queue.enqueue(source_path)
                source = service.process(job.job_id, space=arguments.space)
                current = queue.get(job.job_id)
            if current.state == "pending_attention":
                _print_pending_job(current, config.compiler)
                return 2
            print(f"{source.version_id} {source.status}")
            return 0
        tracker = StableTracker(config.stable_seconds, trusted_root=paths.inbox)
        submitted: set[Path] = set()
        while True:
            for source in watch_once(paths, tracker, submitted, space=arguments.space):
                print(f"{source.version_id} {source.status}")
            time.sleep(config.poll_seconds)
    except Exception as error:
        print(f"kb: {error}", file=sys.stderr)
        return 1

    return 1


def _paths_for_arguments(arguments: argparse.Namespace) -> VaultPaths:
    if arguments.command in {"ingest-once", "watch"}:
        vault_root = Path(arguments.vault).resolve()
    else:
        vault_root = resolve_vault_path(
            getattr(arguments, "vault", None)
        )
    return VaultPaths(vault_root)


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


def _status_report(paths: VaultPaths) -> dict[str, object]:
    if not paths.queue.is_dir():
        raise ValueError("queue directory is missing; run kb init")
    queue = DiskQueue(paths.queue)
    jobs, truncated = queue.iter_jobs_bounded_readonly(
        max_jobs=STATUS_MAX_JOBS,
        max_bytes=STATUS_MAX_BYTES,
        max_entries=STATUS_MAX_ENTRIES,
    )
    actionable = [
        _status_job(job) for job in jobs if job.state != "published"
    ]
    attention_required = bool(actionable) or truncated
    return {
        "schema_version": 1,
        "healthy": not attention_required,
        "attention_required": attention_required,
        "truncated": truncated,
        "actionable_count": len(actionable),
        "actionable_count_is_lower_bound": truncated,
        "jobs": actionable,
    }


def _status_job(job) -> dict[str, object]:
    source = job.metadata.get("source")
    source_id = source.get("source_id") if isinstance(source, dict) else None
    version_id = source.get("version_id") if isinstance(source, dict) else None
    handoff = job.metadata.get("compiler_handoff")
    return {
        "job_id": job.job_id,
        "type": (
            "derived_update"
            if job.metadata.get("job_type") == "derived_update"
            else "source_ingest"
        ),
        "state": job.state,
        "error": job.error,
        "handoff_path": handoff if isinstance(handoff, str) else None,
        "source_id": source_id if isinstance(source_id, str) else None,
        "version_id": version_id if isinstance(version_id, str) else None,
    }


def _print_pending_job(job, compiler_provider: str, *, stream=None) -> None:
    destination = stream or sys.stdout
    provider = compiler_provider.strip().casefold()
    reason = (
        "Claude 不可用，需要人工處理"
        if provider == "claude"
        else "目前設定為手動編譯，需要人工處理"
    )
    handoff = job.metadata.get("compiler_handoff")
    print(f"Job {job.job_id}: pending_attention", file=destination)
    print(reason, file=destination)
    if isinstance(handoff, str):
        print(f"Handoff: {handoff}", file=destination)
    print(
        f"下一步：修正編譯器後執行 kb resume --vault \"VAULT\" --job-id {job.job_id}",
        file=destination,
    )


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
    service = IngestService(
        paths,
        queue,
        Catalog(paths.index / "catalog.sqlite3"),
        compiler=_compiler_for_config(config, paths),
    )
    results = []
    for job in queue.iter_jobs():
        if job.metadata.get("job_type") == "derived_update":
            if job.state in {"published", "pending_attention"}:
                continue
            try:
                service.process_derived_update(job.job_id)
                current = queue.get(job.job_id)
                if current.state == "pending_attention":
                    _print_pending_job(
                        current, config.compiler, stream=sys.stderr
                    )
            except Exception as error:
                print(
                    f"kb watch: derived {job.job_id}: {error}",
                    file=sys.stderr,
                )
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
            print(f"kb watch: {job.job_id}: {error}", file=sys.stderr)
            continue
        current = queue.get(job.job_id)
        if current.state == "pending_attention":
            _print_pending_job(current, config.compiler, stream=sys.stderr)
        results.append(result)
        original = Path(
            str(current.metadata.get("original_source_path", current.source_path))
        )
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
            result = service.process(job.job_id, space=space)
            current = queue.get(job.job_id)
            if current.state == "pending_attention":
                _print_pending_job(
                    current, config.compiler, stream=sys.stderr
                )
            results.append(result)
        except Exception as error:
            print(f"kb watch: {job.job_id}: {error}", file=sys.stderr)
            continue
        submitted.discard(candidate)
        tracker.forget(candidate)
    tracker.prune()
    return results


if __name__ == "__main__":
    raise SystemExit(main())
