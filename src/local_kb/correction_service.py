"""Create and manage evidence-grounded local correction records."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .correction_index import CorrectionIndex
from .correction_model import (
    Applicability,
    CorrectionRecord,
    EvidenceReference,
    canonical_correction_hash,
    validate_record,
)
from .correction_store import CorrectionStore
from .correction_validation import validate_trigger
from .paths import VaultPaths
from .query import evidence_sha256
from .queue import WriterLock


@dataclass(frozen=True)
class CreateResult:
    record: CorrectionRecord
    created: bool
    occurrence_event_id: str


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(f"{label} must be a list of strings")
    return tuple(value)


def _grounded_evidence(
    packet: dict[str, object],
    proposal: dict[str, object],
) -> tuple[EvidenceReference, ...]:
    packet_evidence = packet.get("evidence")
    proposed = proposal.get("supporting_evidence")
    if not isinstance(packet_evidence, list) or not isinstance(proposed, list):
        raise ValueError("supporting evidence must be a list")
    allowed = {}
    for item in packet_evidence:
        if not isinstance(item, dict) or item.get("kind") != "raw_fragment":
            continue
        try:
            digest = item["evidence_sha256"]
            identity = (
                item["source_id"],
                item["version_id"],
                item["locator"],
                digest,
            )
        except KeyError:
            continue
        if (
            isinstance(digest, str)
            and digest == evidence_sha256(item)
            and item.get("space") in packet.get("spaces", [])
        ):
            allowed[identity] = item
    result = []
    required = {
        "source_id",
        "version_id",
        "locator",
        "evidence_sha256",
    }
    for item in proposed:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("supporting evidence has invalid fields")
        identity = tuple(item[key] for key in (
            "source_id",
            "version_id",
            "locator",
            "evidence_sha256",
        ))
        if identity not in allowed:
            raise ValueError(
                "supporting evidence is not exact raw packet evidence"
            )
        result.append(EvidenceReference(**item))
    if not result or len(set(result)) != len(result):
        raise ValueError("supporting evidence is empty or duplicated")
    return tuple(result)


def _record_from_proposal(
    packet: dict[str, object],
    proposal: dict[str, object],
) -> CorrectionRecord:
    if packet.get("schema_version") != 2:
        raise ValueError("unsupported packet schema")
    question = packet.get("question")
    spaces = packet.get("spaces")
    if (
        not isinstance(question, str)
        or not question.strip()
        or not isinstance(spaces, list)
        or not spaces
    ):
        raise ValueError("packet question or spaces are invalid")
    validate_trigger(packet, proposal)
    applicability_value = proposal.get("applicability")
    fields = {
        "spaces",
        "file_types",
        "source_families",
        "sheet_names",
        "column_names",
        "units",
        "question_types",
        "keywords",
        "error_types",
    }
    if (
        not isinstance(applicability_value, dict)
        or set(applicability_value) != fields
    ):
        raise ValueError("applicability must contain exact fields")
    applicability = Applicability(**{
        field: _strings(applicability_value[field], field)
        for field in fields
    })
    if any(space not in spaces for space in applicability.spaces):
        raise ValueError("correction space is outside the packet")
    supporting = _grounded_evidence(packet, proposal)
    now = _now()
    record = CorrectionRecord(
        correction_id=f"COR-{now[:10].replace('-', '')}-{uuid4().hex[:12]}",
        schema_version=1,
        status="active",
        created_at=now,
        updated_at=now,
        trigger_type=proposal.get("trigger_type"),
        created_by=proposal.get("created_by"),
        original_question=question,
        wrong_answer_summary=proposal.get("wrong_answer_summary"),
        error_type=proposal.get("error_type"),
        correction_rule=proposal.get("correction_rule"),
        applicability=applicability,
        exclusions=_strings(proposal.get("exclusions"), "exclusions"),
        supporting_evidence=supporting,
        validated_versions=tuple(dict.fromkeys(
            reference.version_id for reference in supporting
        )),
        supersedes=(),
        superseded_by=(),
        content_sha256="",
    )
    record = CorrectionRecord(
        **{
            **record.__dict__,
            "content_sha256": canonical_correction_hash(record),
        }
    )
    return validate_record(record)


def _normalized_dedupe(record: CorrectionRecord) -> str:
    payload = {
        "error_type": record.error_type,
        "correction_rule": record.correction_rule.strip().casefold(),
        "applicability": {
            key: sorted(item.strip().casefold() for item in value)
            for key, value in asdict(record.applicability).items()
        },
        "exclusions": sorted(
            item.strip().casefold() for item in record.exclusions
        ),
        "supporting_evidence": sorted(
            (
                item.source_id,
                item.version_id,
                item.locator,
                item.evidence_sha256,
            )
            for item in record.supporting_evidence
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CorrectionService:
    def __init__(self, vault: VaultPaths | Path | str) -> None:
        self.paths = (
            vault
            if isinstance(vault, VaultPaths)
            else VaultPaths(Path(vault).absolute())
        )
        self.store = CorrectionStore(self.paths)
        self.index = CorrectionIndex(self.paths)

    def create(
        self,
        packet: dict[str, object],
        proposal: dict[str, object],
    ) -> CreateResult:
        if not isinstance(packet, dict) or not isinstance(proposal, dict):
            raise TypeError("packet and proposal must be objects")
        proposed = _record_from_proposal(packet, proposal)
        dedupe = _normalized_dedupe(proposed)
        event_type = "created"
        created = True
        index_error = None
        with WriterLock(self.paths.runtime / "write.lock", timeout=0):
            records, truncated = self.store.iter_records()
            if truncated:
                raise ValueError("correction dedupe scan was truncated")
            record = next(
                (
                    item
                    for item in records
                    if _normalized_dedupe(item) == dedupe
                    and item.status != "retired"
                ),
                None,
            )
            if record is None:
                record = self.store.create(proposed)
            else:
                created = False
                event_type = "occurrence"
            try:
                self.index.upsert(record)
            except Exception as error:
                index_error = error
        if index_error is not None:
            self.store.append_event(
                record.correction_id,
                event_type="index_update_failed",
                actor=str(proposal.get("created_by", "agent"))[:100],
                reason="correction index update failed",
                details={"error": str(index_error)[:500]},
            )
            raise RuntimeError(
                "correction saved but index update failed; run kb rebuild"
            ) from index_error
        event = self.store.append_event(
            record.correction_id,
            event_type=event_type,
            actor=str(proposal.get("created_by", "agent"))[:100],
            reason=str(proposal.get("trigger_type")),
            details={
                "question": str(packet.get("question"))[:16_000],
                "content_sha256": record.content_sha256,
            },
        )
        return CreateResult(
            record=record,
            created=created,
            occurrence_event_id=str(event["event_id"]),
        )

    def list_records(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[CorrectionRecord]:
        records, truncated = self.store.iter_records(max_records=limit)
        if truncated:
            raise ValueError("correction list is truncated")
        return [
            record
            for record in records
            if status is None or record.status == status
        ]
