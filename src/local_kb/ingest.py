"""Turn one durable inbox job into raw evidence, catalog rows and cache data."""

from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

from .catalog import Catalog
from .extractors import registry as default_registry
from .models import Job, SourceVersion
from .paths import VaultPaths
from .queue import DiskQueue
from .source_store import SourceStore, file_sha256


class IngestService:
    def __init__(self, vault: VaultPaths | Path | str, queue: DiskQueue, catalog: Catalog, *, registry: Any = None) -> None:
        self.vault = vault if isinstance(vault, VaultPaths) else VaultPaths(Path(vault).resolve())
        self.queue = queue
        self.catalog = catalog
        self.registry = registry or default_registry
        self.store = SourceStore(self.vault.raw)

    def process(self, job_id: str, *, space: str = "unclassified") -> SourceVersion:
        """Process one job; persist each recoverable boundary before advancing."""
        self.catalog.initialize()
        job = self.queue.get(job_id)
        try:
            source = self._source_for(job)
            if source is None:
                source = self._archive(job, space)
                job = self.queue.get(job_id)
            extraction = self._extraction_for(job, source)
            final = replace(source, status=extraction["status"])
            fragments = [(item["locator"], item["text"]) for item in extraction["fragments"]]
            self.catalog.upsert_source(final, fragments)
            self._mark(job_id, "compiled", source=asdict(final))
            self._write_cache(final, extraction)
            processed = self._move_processed(self.queue.get(job_id), final)
            self._mark(job_id, "published", source=asdict(final), processed_path=str(processed.relative_to(self.vault.root)))
            return final
        except BaseException as error:
            self.queue.fail(job_id, error)
            raise

    def _source_for(self, job: Job) -> SourceVersion | None:
        data = job.metadata.get("source")
        if not isinstance(data, dict):
            return None
        try:
            source = SourceVersion(**data)
        except TypeError:
            return None
        return source if (self.vault.root / source.relative_path).is_file() else None

    def _archive(self, job: Job, space: str) -> SourceVersion:
        incoming = Path(job.source_path)
        latest = self.catalog.latest_source(space, incoming.name)
        source = self.store.archive(incoming, space, source_id=latest.source_id if latest else None, previous_version_id=latest.version_id if latest else None)
        self._mark(job.job_id, "archived", source=asdict(source), space=space)
        return source

    def _extraction_for(self, job: Job, source: SourceVersion) -> dict[str, Any]:
        existing = job.metadata.get("extraction")
        if isinstance(existing, dict) and self._valid_extraction(existing):
            return existing
        result = self.registry.extract(self.vault.root / source.relative_path)
        extraction = {"status": result.status, "warning": result.warning, "fragments": [asdict(fragment) for fragment in result.fragments]}
        if not self._valid_extraction(extraction):
            raise ValueError("extractor returned an invalid extraction")
        self._mark(job.job_id, "extracted", extraction=extraction)
        return extraction

    @staticmethod
    def _valid_extraction(value: dict[str, Any]) -> bool:
        return value.get("status") in {"extracted", "pending_extractor"} and isinstance(value.get("fragments"), list) and all(isinstance(item, dict) and isinstance(item.get("locator"), str) and isinstance(item.get("text"), str) for item in value["fragments"])

    def _mark(self, job_id: str, state: str, **metadata: object) -> None:
        def apply(job: Job) -> None:
            job.state = state  # type: ignore[assignment]
            job.metadata.update(metadata)
            job.error = None
        self.queue.update(job_id, apply)

    def _write_cache(self, source: SourceVersion, extraction: dict[str, Any]) -> None:
        directory = self.vault.index / "cache"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{source.version_id}.json"
        temporary = directory / f".{target.name}.{uuid4().hex}.tmp"
        payload = {"source": asdict(source), "fragments": extraction["fragments"], "warning": extraction.get("warning")}
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            self._sync_directory(directory)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _move_processed(self, job: Job, source: SourceVersion) -> Path:
        self.queue._validate_job_id(job.job_id)
        name = Path(job.source_path).name
        if not name or name in {".", ".."}:
            raise ValueError("source path must have a safe filename")
        target = self.vault.trash / "processed-inbox" / job.job_id / name
        target.parent.mkdir(parents=True, exist_ok=True)
        incoming = Path(job.source_path)
        if target.exists():
            if incoming.exists() and file_sha256(target) != file_sha256(incoming):
                raise FileExistsError("processed target contains different file")
            return target
        if not incoming.exists():
            raise FileNotFoundError(f"incoming file is missing: {incoming}")
        shutil.move(str(incoming), str(target))
        return target

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
