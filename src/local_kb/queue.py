"""Small durable on-disk queue for inbox ingestion jobs."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import time
from typing import Callable, Iterator, get_args
from uuid import uuid4

from .models import Job, JobState


_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_JOB_STATES = frozenset(get_args(JobState))


class DiskQueue:
    """Persist jobs as atomically-replaced JSON files.

    The queue lock covers read-modify-write operations, so two worker processes
    cannot lose a retry increment even though each job is a separate file.
    """

    def __init__(self, root: Path | str, max_retries: int = 3) -> None:
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 1:
            raise ValueError("max_retries must be a positive integer")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_retries = max_retries
        self._lock_path = self.root / ".queue.lock"

    def enqueue(self, source_path: Path | str, *, job_id: str | None = None) -> Job:
        identifier = job_id or uuid4().hex
        self._validate_job_id(identifier)
        job = Job(job_id=identifier, source_path=os.fspath(source_path))
        with self._locked():
            path = self._job_path(identifier)
            if path.exists():
                raise FileExistsError(f"job already exists: {identifier}")
            self._write(path, job)
        return self._copy(job)

    def get(self, job_id: str) -> Job:
        with self._locked():
            return self._read(self._job_path(job_id))

    def iter_jobs(self) -> list[Job]:
        with self._locked():
            return [self._read(path) for path in sorted(self.root.glob("*.json"))]

    def active_for_source(self, source_path: Path | str) -> Job | None:
        wanted = os.path.normcase(os.path.abspath(os.fspath(source_path)))
        for job in self.iter_jobs():
            original = job.metadata.get("original_source_path", job.source_path)
            if job.state not in {"pending_attention", "published"} and os.path.normcase(os.path.abspath(str(original))) == wanted:
                return job
        return None

    def update(self, job_id: str, change: Callable[[Job], Job | None]) -> Job:
        """Atomically apply *change* to a fresh job copy and persist it."""
        with self._locked():
            path = self._job_path(job_id)
            job = self._read(path)
            result = change(job)
            if result is not None:
                job = result
            if job.job_id != job_id:
                raise ValueError("job update cannot change job_id")
            self._write(path, job)
            return self._copy(job)

    def fail(self, job_id: str, error: BaseException) -> Job:
        message = str(error) or error.__class__.__name__

        def record(job: Job) -> None:
            job.attempts += 1
            job.error = message
            job.state = (
                "pending_attention" if job.attempts >= self.max_retries else "retrying"
            )

        return self.update(job_id, record)

    def _job_path(self, job_id: str) -> Path:
        self._validate_job_id(job_id)
        return self.root / f"{job_id}.json"

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise ValueError("job_id must be a canonical safe identifier")

    def _read(self, path: Path) -> Job:
        try:
            with path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            raise
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"corrupt job JSON: {path.name}") from error
        if not isinstance(data, dict):
            raise ValueError(f"corrupt job JSON: {path.name}")
        required = {"job_id", "source_path", "state", "attempts", "error", "metadata"}
        if set(data) != required:
            raise ValueError(f"corrupt job JSON: {path.name}")
        try:
            job = Job(**data)
        except (TypeError, ValueError) as error:
            raise ValueError(f"corrupt job JSON: {path.name}") from error
        self._validate_job(job, path.name)
        return self._copy(job)

    def _write(self, path: Path, job: Job) -> None:
        self._validate_job(job, path.name)
        payload = json.dumps(job.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        temporary = self.root / f".{path.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._sync_directory()
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _sync_directory(self) -> None:
        if os.name == "nt":
            return
        try:
            descriptor = os.open(self.root, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._lock_path.touch(exist_ok=True)
        with self._lock_path.open("a+b") as stream:
            if os.name == "nt":
                import msvcrt

                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                while True:
                    try:
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.01)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _copy(job: Job) -> Job:
        return Job(**json.loads(json.dumps(job.to_dict())))

    def _validate_job(self, job: Job, filename: str) -> None:
        if (
            not isinstance(job, Job)
            or not isinstance(job.source_path, str)
            or not isinstance(job.state, str)
            or job.state not in _JOB_STATES
            or isinstance(job.attempts, bool)
            or not isinstance(job.attempts, int)
            or job.attempts < 0
            or job.error is not None and not isinstance(job.error, str)
            or not isinstance(job.metadata, dict)
        ):
            raise ValueError(f"corrupt job JSON: {filename}")
        self._validate_job_id(job.job_id)
        if filename != f"{job.job_id}.json":
            raise ValueError(f"corrupt job JSON: {filename}")
        try:
            json.dumps(job.to_dict(), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError(f"corrupt job JSON: {filename}") from error
