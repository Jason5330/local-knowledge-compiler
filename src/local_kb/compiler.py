"""Model-neutral evidence compiler adapters.

The Claude adapter is deliberately a narrow, non-interactive subprocess.  Any
failure to obtain a small, structured response becomes a durable manual handoff
instead of an unverified knowledge-base update.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import threading
import time
from typing import Any
from uuid import uuid4


MAX_EVIDENCE_CHARS = 2_100_000
MAX_PROMPT_CHARS = 600_000
MAX_OUTPUT_BYTES = 1_000_000
MAX_CHANGES = 100
MAX_SOURCE_IDS_PER_CHANGE = 8

# Kept as plain English source text so every terminal can inspect this safety
# boundary without locale-dependent rendering.  User-facing answers remain
# Traditional Chinese elsewhere in the system.
CLAUDE_PROMPT_INSTRUCTIONS = (
    "Use only the following local evidence to propose Wiki changes. "
    "Do not browse the web and do not add model background knowledge. "
    "Every factual claim must preserve its source_id. "
    "Return an empty changes array when the evidence is insufficient."
)

_CHANGE_FIELDS = frozenset({
    "path", "title", "type", "space", "confidence", "source_ids",
    "current_state", "conflicts", "timeline_entry",
})
_OUTPUT_SCHEMA_DATA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "changes": {
            "type": "array",
            "maxItems": MAX_CHANGES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "title": {"type": "string"},
                    "type": {"type": "string"},
                    "space": {"type": "string"},
                    "confidence": {"enum": ["high", "medium", "low"]},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                    "current_state": {"type": "string"},
                    "conflicts": {"type": "string"},
                    "timeline_entry": {"type": "string"},
                },
                "required": sorted(_CHANGE_FIELDS),
            },
        },
    },
    "required": ["changes"],
}
OUTPUT_SCHEMA = json.dumps(_OUTPUT_SCHEMA_DATA, ensure_ascii=False, separators=(",", ":"))


def _controlled_environment() -> dict[str, str]:
    """Pass only runtime essentials to an external CLI, plus safe defaults."""
    allowed = (
        "PATH", "PATHEXT", "SystemRoot", "WINDIR", "COMSPEC", "USERPROFILE", "HOME",
        "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "LANG", "LC_ALL",
    )
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.update({"CI": "true", "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"})
    return environment


def _safe_reason(error: object) -> str:
    value = str(error).replace("\r", " ").replace("\n", " ")
    return value[:500] or "compiler returned an invalid result"


def _valid_payload_shape(payload: object) -> bool:
    """Reject malformed CLI output before it can reach the wiki transaction."""
    if not isinstance(payload, dict) or set(payload) != {"changes"}:
        return False
    changes = payload["changes"]
    if not isinstance(changes, list) or len(changes) > MAX_CHANGES:
        return False
    for change in changes:
        if not isinstance(change, dict) or set(change) != _CHANGE_FIELDS:
            return False
        if not all(isinstance(change[field], str) for field in _CHANGE_FIELDS - {"source_ids"}):
            return False
        source_ids = change["source_ids"]
        if (not isinstance(source_ids, list) or not source_ids
                or len(source_ids) > MAX_SOURCE_IDS_PER_CHANGE
                or not all(isinstance(source_id, str) for source_id in source_ids)):
            return False
    return True


def _terminate_process_tree(process: subprocess.Popen) -> None:
    """Best-effort termination of the isolated Claude process group and children."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            killer = subprocess.Popen(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            killer.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _run_bounded_process(
    command: list[str], prompt: str | bytes | None, *, cwd: Path, timeout: float,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> tuple[int, bytes, bool, bool]:
    """Run with bounded combined output; return code, stdout, overflow, timeout."""
    if (not isinstance(max_output_bytes, int) or isinstance(max_output_bytes, bool)
            or max_output_bytes <= 0):
        raise ValueError("max_output_bytes must be a positive integer")
    platform_options: dict[str, Any]
    if os.name == "nt":
        platform_options = {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        }
    else:
        platform_options = {"start_new_session": True}
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd),
        env=_controlled_environment(),
        shell=False,
        **platform_options,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    lock = threading.Lock()
    overflow = threading.Event()

    def read_stream(name: str, stream) -> None:
        try:
            while chunk := stream.read(64 * 1024):
                with lock:
                    used = len(buffers["stdout"]) + len(buffers["stderr"])
                    remaining = max(0, max_output_bytes - used)
                    if remaining:
                        buffers[name].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        overflow.set()
                        return
        except (OSError, ValueError):
            return

    def write_prompt() -> None:
        try:
            input_bytes = prompt.encode("utf-8") if isinstance(prompt, str) else (prompt or b"")
            process.stdin.write(input_bytes)
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    readers = [
        threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
    ]
    writer = threading.Thread(target=write_prompt, daemon=True)
    for thread in readers:
        thread.start()
    writer.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    returncode = -1
    try:
        while process.poll() is None:
            if overflow.wait(timeout=min(0.02, max(0.0, deadline - time.monotonic()))):
                _terminate_process_tree(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_process_tree(process)
                break
        try:
            returncode = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            observed = process.poll()
            returncode = -1 if observed is None else observed
    finally:
        writer.join(timeout=1)
        if writer.is_alive():
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            writer.join(timeout=1)
        # A normally-exited child closes both pipes.  Let readers drain those
        # kernel buffers before closing handles or snapshotting the bytearrays.
        for thread in readers:
            thread.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            if any(thread.is_alive() for thread in readers):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        for thread in readers:
            thread.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
    return int(returncode), bytes(buffers["stdout"]), overflow.is_set(), timed_out


class ManualCompiler:
    """Create a durable handoff packet without claiming any wiki was updated."""

    def __init__(
        self, outbox: Path | str, *, trusted_root: Path | str | None = None
    ) -> None:
        supplied_outbox = Path(outbox)
        if trusted_root is None:
            absolute_outbox = Path(os.path.abspath(os.fspath(supplied_outbox)))
            supplied_root = absolute_outbox.parent
            supplied_outbox = absolute_outbox
        else:
            supplied_root = Path(trusted_root)
        if not supplied_root.is_absolute() or not supplied_outbox.is_absolute():
            raise ValueError("manual compiler paths must be absolute")
        self.trusted_root = Path(os.path.abspath(os.fspath(supplied_root)))
        self.outbox = Path(os.path.abspath(os.fspath(supplied_outbox)))
        try:
            relative = self.outbox.relative_to(self.trusted_root)
        except ValueError as error:
            raise ValueError("manual outbox must be beneath trusted_root") from error
        for component in relative.parts:
            stem = component.rstrip(". ").split(".", 1)[0].upper()
            if (not component or component in {".", ".."} or component != component.rstrip(". ")
                    or ":" in component or any(ord(character) < 32 for character in component)
                    or stem in {"CON", "PRN", "AUX", "NUL"}
                    or re.fullmatch(r"(?:COM|LPT)[1-9]", stem)):
                raise ValueError("manual outbox contains an unsafe path component")

    def compile(self, evidence: str, *, reason: str | None = None) -> Path:
        if not isinstance(evidence, str) or len(evidence) > MAX_EVIDENCE_CHARS:
            raise ValueError("evidence exceeds the manual handoff budget")
        packet: dict[str, Any] = {
            "schema_version": 1,
            "status": "needs_agent",
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "evidence": evidence,
            "output_schema": _OUTPUT_SCHEMA_DATA,
            "instructions": (
                "Review only this local evidence. Produce a separate JSON result that "
                "matches output_schema; it has not been published to the wiki."
            ),
        }
        if reason:
            packet["reason"] = _safe_reason(reason)
        encoded = (json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        with self._pinned_outbox() as directory_fd:
            for _ in range(16):
                identifier = uuid4().hex
                temporary_name = f".manual-{identifier}.tmp"
                target_name = f"manual_{identifier}.json"
                descriptor: int | None = None
                try:
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                    if directory_fd is not None:
                        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
                    else:
                        descriptor = os.open(self.outbox / temporary_name, flags, 0o600)
                    offset = 0
                    while offset < len(encoded):
                        offset += os.write(descriptor, encoded[offset:])
                    os.fsync(descriptor)
                    os.close(descriptor)
                    descriptor = None
                    try:
                        if directory_fd is not None:
                            os.link(
                                temporary_name, target_name,
                                src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                                follow_symlinks=False,
                            )
                        else:
                            os.link(self.outbox / temporary_name, self.outbox / target_name)
                    except FileExistsError:
                        continue
                    if directory_fd is not None:
                        os.fsync(directory_fd)
                    return self.outbox / target_name
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                    try:
                        if directory_fd is not None:
                            os.unlink(temporary_name, dir_fd=directory_fd)
                        else:
                            (self.outbox / temporary_name).unlink(missing_ok=True)
                    except FileNotFoundError:
                        pass
        raise RuntimeError("unable to allocate a unique manual handoff path")

    @contextmanager
    def _pinned_outbox(self):
        if os.name == "nt":
            from .source_store import _windows_close_handle, _windows_open_directory

            handles: list[int] = []
            try:
                handles.append(_windows_open_directory(self.trusted_root, self.trusted_root))
                cursor = self.trusted_root
                for component in self.outbox.relative_to(self.trusted_root).parts:
                    cursor = cursor / component
                    if os.path.lexists(cursor):
                        info = os.lstat(cursor)
                        junction = getattr(cursor, "is_junction", None)
                        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                                or (callable(junction) and junction())):
                            raise ValueError("manual outbox path is unsafe")
                    else:
                        os.mkdir(cursor)
                    handles.append(_windows_open_directory(cursor, self.trusted_root))
                yield None
            finally:
                for handle in reversed(handles):
                    _windows_close_handle(handle)
            return

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptors: list[int] = []
        try:
            anchor = Path(self.trusted_root.anchor)
            descriptor = os.open(anchor, flags)
            descriptors.append(descriptor)
            for component in self.trusted_root.parts[1:]:
                descriptor = os.open(component, flags, dir_fd=descriptor)
                if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise ValueError("trusted_root path is unsafe")
                descriptors.append(descriptor)
            for component in self.outbox.relative_to(self.trusted_root).parts:
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                    child = os.open(component, flags, dir_fd=descriptor)
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    raise ValueError("manual outbox path is unsafe")
                descriptors.append(child)
                descriptor = child
            yield descriptor
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


class ClaudeCompiler:
    """Run Claude in print mode and safely downgrade failures to a manual job."""

    def __init__(
        self,
        *,
        fallback: ManualCompiler | None = None,
        cwd: Path | str | None = None,
        timeout: float = 120.0,
    ) -> None:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("timeout must be positive")
        self.cwd = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()
        self.fallback = fallback or ManualCompiler(
            self.cwd / ".kb" / "manual", trusted_root=self.cwd,
        )
        self.timeout = float(timeout)

    def compile(self, evidence: str) -> dict[str, Any] | Path:
        if not isinstance(evidence, str) or len(evidence) > MAX_EVIDENCE_CHARS:
            return self.fallback.compile("", reason="evidence exceeds compiler budget")
        prompt = CLAUDE_PROMPT_INSTRUCTIONS + "\n\n" + evidence
        if len(prompt) > MAX_PROMPT_CHARS:
            return self.fallback.compile(evidence, reason="evidence exceeds Claude prompt budget")
        command = [
            "claude", "-p", "--output-format", "json", "--permission-mode", "dontAsk",
            "--tools", "", "--no-session-persistence", "--json-schema", OUTPUT_SCHEMA,
        ]
        try:
            returncode, stdout_bytes, overflowed, timed_out = _run_bounded_process(
                command, prompt, cwd=self.cwd, timeout=self.timeout,
            )
        except OSError as error:
            return self.fallback.compile(evidence, reason=f"Claude CLI unavailable: {_safe_reason(error)}")
        if timed_out:
            return self.fallback.compile(evidence, reason="Claude CLI timed out")
        if overflowed:
            return self.fallback.compile(evidence, reason="Claude CLI output exceeds the safe size limit")
        if returncode != 0:
            return self.fallback.compile(evidence, reason="Claude CLI returned a non-zero exit status")
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        try:
            envelope = json.loads(stdout)
            payload = envelope["result"]
            if isinstance(payload, str):
                if len(payload.encode("utf-8")) > MAX_OUTPUT_BYTES:
                    raise ValueError("result exceeds the safe size limit")
                payload = json.loads(payload)
            if not _valid_payload_shape(payload):
                raise ValueError("result does not match the required change schema")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            return self.fallback.compile(evidence, reason=f"Claude CLI returned malformed JSON: {_safe_reason(error)}")
        return payload
