"""Model-neutral evidence compiler adapters.

The Claude adapter is deliberately a narrow, non-interactive subprocess.  Any
failure to obtain a small, structured response becomes a durable manual handoff
instead of an unverified knowledge-base update.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
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


class ManualCompiler:
    """Create a durable handoff packet without claiming any wiki was updated."""

    def __init__(self, outbox: Path | str) -> None:
        self.outbox = Path(outbox)

    def compile(self, evidence: str, *, reason: str | None = None) -> Path:
        if not isinstance(evidence, str) or len(evidence) > MAX_EVIDENCE_CHARS:
            raise ValueError("evidence exceeds the manual handoff budget")
        self.outbox.mkdir(parents=True, exist_ok=True)
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
        for _ in range(16):
            target = self.outbox / f"manual_{uuid4().hex}.json"
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".manual-", suffix=".tmp", dir=self.outbox,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    # A hard link makes publication atomic and refuses an existing name.
                    os.link(temporary, target)
                except FileExistsError:
                    continue
                if os.name != "nt":
                    directory_fd = os.open(self.outbox, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                return target
            finally:
                temporary.unlink(missing_ok=True)
        raise RuntimeError("unable to allocate a unique manual handoff path")


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
        self.fallback = fallback or ManualCompiler(self.cwd / ".kb" / "manual")
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
            result = subprocess.run(
                command,
                input=prompt,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self.cwd),
                env=_controlled_environment(),
                shell=False,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return self.fallback.compile(evidence, reason=f"Claude CLI unavailable: {_safe_reason(error)}")
        if getattr(result, "returncode", 0) != 0:
            return self.fallback.compile(evidence, reason="Claude CLI returned a non-zero exit status")
        stdout = getattr(result, "stdout", "")
        if not isinstance(stdout, str) or len(stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
            return self.fallback.compile(evidence, reason="Claude CLI output exceeds the safe size limit")
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
