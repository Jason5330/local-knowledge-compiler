"""Strict, non-executable correction records."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Literal


CorrectionStatus = Literal["active", "stale", "suspended", "retired"]
TriggerType = Literal[
    "user_reported_wrong",
    "deterministic_validation_failure",
]
ErrorType = Literal[
    "extraction_error",
    "retrieval_error",
    "unit_error",
    "time_error",
    "range_error",
    "reasoning_error",
    "citation_error",
]

STATUSES = frozenset({"active", "stale", "suspended", "retired"})
TRIGGERS = frozenset(
    {"user_reported_wrong", "deterministic_validation_failure"}
)
ERROR_TYPES = frozenset(
    {
        "extraction_error",
        "retrieval_error",
        "unit_error",
        "time_error",
        "range_error",
        "reasoning_error",
        "citation_error",
    }
)
MAX_RULE_CHARS = 4_000
MAX_LIST_ITEMS = 32
_ID = re.compile(r"COR-[0-9]{8}-[0-9a-f]{12}\Z")
_ENTITY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_TOKEN = re.compile(r"[^\x00-\x1f\x7f]{1,200}\Z")
_FORBIDDEN_RULES = (
    "ignore previous",
    "忽略先前",
    "search the web",
    "搜尋網路",
    "run command",
    "執行指令",
)
_RECORD_FIELDS = frozenset(
    {
        "correction_id",
        "schema_version",
        "status",
        "created_at",
        "updated_at",
        "trigger_type",
        "created_by",
        "original_question",
        "wrong_answer_summary",
        "error_type",
        "correction_rule",
        "applicability",
        "exclusions",
        "supporting_evidence",
        "validated_versions",
        "supersedes",
        "superseded_by",
        "content_sha256",
    }
)
_APPLICABILITY_FIELDS = frozenset(
    {
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
)
_EVIDENCE_FIELDS = frozenset(
    {"source_id", "version_id", "locator", "evidence_sha256"}
)


@dataclass(frozen=True)
class EvidenceReference:
    source_id: str
    version_id: str
    locator: str
    evidence_sha256: str


@dataclass(frozen=True)
class Applicability:
    spaces: tuple[str, ...]
    file_types: tuple[str, ...] = ()
    source_families: tuple[str, ...] = ()
    sheet_names: tuple[str, ...] = ()
    column_names: tuple[str, ...] = ()
    units: tuple[str, ...] = ()
    question_types: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    error_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorrectionRecord:
    correction_id: str
    schema_version: int
    status: CorrectionStatus
    created_at: str
    updated_at: str
    trigger_type: TriggerType
    created_by: str
    original_question: str
    wrong_answer_summary: str
    error_type: ErrorType
    correction_rule: str
    applicability: Applicability
    exclusions: tuple[str, ...]
    supporting_evidence: tuple[EvidenceReference, ...]
    validated_versions: tuple[str, ...]
    supersedes: tuple[str, ...]
    superseded_by: tuple[str, ...]
    content_sha256: str


def canonical_correction_hash(record: CorrectionRecord) -> str:
    payload = asdict(record)
    payload["content_sha256"] = ""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_text(
    value: str,
    label: str,
    *,
    maximum: int,
    empty: bool = False,
) -> None:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{label} is invalid")
    if not empty and not value.strip():
        raise ValueError(f"{label} is empty")
    if "\x00" in value or any(
        ord(character) == 127
        or (ord(character) < 32 and character not in {"\n", "\t"})
        for character in value
    ):
        raise ValueError(f"{label} contains a control character")


def _timestamp(value: str, label: str) -> None:
    _safe_text(value, label, maximum=40)
    if not value.endswith("Z"):
        raise ValueError(f"{label} must be UTC")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{label} is invalid") from error


def _tokens(values: tuple[str, ...], label: str) -> None:
    if len(values) > MAX_LIST_ITEMS or len(set(values)) != len(values):
        raise ValueError(f"{label} is too large or contains duplicates")
    if any(
        not isinstance(value, str) or _SAFE_TOKEN.fullmatch(value) is None
        for value in values
    ):
        raise ValueError(f"{label} contains an unsafe token")


def validate_record(record: CorrectionRecord) -> CorrectionRecord:
    if record.schema_version != 1:
        raise ValueError("unsupported correction schema")
    if _ID.fullmatch(record.correction_id) is None:
        raise ValueError("invalid correction_id")
    if record.status not in STATUSES:
        raise ValueError("invalid correction status")
    if record.trigger_type not in TRIGGERS:
        raise ValueError("invalid correction trigger")
    if record.error_type not in ERROR_TYPES:
        raise ValueError("invalid correction error type")
    _timestamp(record.created_at, "created_at")
    _timestamp(record.updated_at, "updated_at")
    _safe_text(record.created_by, "created_by", maximum=100)
    _safe_text(
        record.original_question,
        "original_question",
        maximum=16_000,
    )
    _safe_text(
        record.wrong_answer_summary,
        "wrong_answer_summary",
        maximum=16_000,
    )
    _safe_text(
        record.correction_rule,
        "correction_rule",
        maximum=MAX_RULE_CHARS,
    )
    lowered = record.correction_rule.casefold()
    if any(marker in lowered for marker in _FORBIDDEN_RULES):
        raise ValueError("correction contains an unsafe instruction")
    applicability_values = (
        record.applicability.spaces,
        record.applicability.file_types,
        record.applicability.source_families,
        record.applicability.sheet_names,
        record.applicability.column_names,
        record.applicability.units,
        record.applicability.question_types,
        record.applicability.keywords,
        record.applicability.error_types,
    )
    for label, values in zip(
        sorted(_APPLICABILITY_FIELDS),
        applicability_values,
        strict=True,
    ):
        _tokens(values, label)
    if len(record.applicability.spaces) != 1:
        raise ValueError("correction must belong to exactly one space")
    anchors = (
        record.applicability.source_families
        + record.applicability.sheet_names
        + record.applicability.column_names
        + record.applicability.units
    )
    if not anchors:
        raise ValueError("correction requires a structural anchor")
    if not record.supporting_evidence:
        raise ValueError("correction requires supporting evidence")
    if len(record.supporting_evidence) > MAX_LIST_ITEMS:
        raise ValueError("too much supporting evidence")
    for reference in record.supporting_evidence:
        if (
            _ENTITY_ID.fullmatch(reference.source_id) is None
            or _ENTITY_ID.fullmatch(reference.version_id) is None
            or _DIGEST.fullmatch(reference.evidence_sha256) is None
        ):
            raise ValueError("supporting evidence identity is invalid")
        _safe_text(
            reference.locator,
            "supporting evidence locator",
            maximum=2_000,
        )
        if "\n" in reference.locator or "\t" in reference.locator:
            raise ValueError("supporting evidence locator must be one line")
    _tokens(record.exclusions, "exclusions")
    _tokens(record.validated_versions, "validated_versions")
    _tokens(record.supersedes, "supersedes")
    _tokens(record.superseded_by, "superseded_by")
    if any(_ID.fullmatch(item) is None for item in record.supersedes):
        raise ValueError("supersedes contains an invalid correction_id")
    if any(_ID.fullmatch(item) is None for item in record.superseded_by):
        raise ValueError("superseded_by contains an invalid correction_id")
    if record.content_sha256 != canonical_correction_hash(record):
        raise ValueError("correction content hash does not match")
    return record


def record_to_dict(record: CorrectionRecord) -> dict[str, object]:
    validate_record(record)
    return asdict(record)


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(f"{label} must be a list of strings")
    return tuple(value)


def record_from_dict(value: object) -> CorrectionRecord:
    if not isinstance(value, dict) or set(value) != _RECORD_FIELDS:
        raise ValueError("correction record must contain exact fields")
    applicability_value = value["applicability"]
    if (
        not isinstance(applicability_value, dict)
        or set(applicability_value) != _APPLICABILITY_FIELDS
    ):
        raise ValueError("applicability must contain exact fields")
    evidence_value = value["supporting_evidence"]
    if not isinstance(evidence_value, (list, tuple)):
        raise ValueError("supporting_evidence must be a list")
    evidence = []
    for item in evidence_value:
        if not isinstance(item, dict) or set(item) != _EVIDENCE_FIELDS:
            raise ValueError("supporting evidence must contain exact fields")
        evidence.append(
            EvidenceReference(
                source_id=item["source_id"],
                version_id=item["version_id"],
                locator=item["locator"],
                evidence_sha256=item["evidence_sha256"],
            )
        )
    applicability = Applicability(
        **{
            field: _string_tuple(applicability_value[field], field)
            for field in _APPLICABILITY_FIELDS
        }
    )
    try:
        record = CorrectionRecord(
            correction_id=value["correction_id"],
            schema_version=value["schema_version"],
            status=value["status"],
            created_at=value["created_at"],
            updated_at=value["updated_at"],
            trigger_type=value["trigger_type"],
            created_by=value["created_by"],
            original_question=value["original_question"],
            wrong_answer_summary=value["wrong_answer_summary"],
            error_type=value["error_type"],
            correction_rule=value["correction_rule"],
            applicability=applicability,
            exclusions=_string_tuple(value["exclusions"], "exclusions"),
            supporting_evidence=tuple(evidence),
            validated_versions=_string_tuple(
                value["validated_versions"],
                "validated_versions",
            ),
            supersedes=_string_tuple(value["supersedes"], "supersedes"),
            superseded_by=_string_tuple(
                value["superseded_by"],
                "superseded_by",
            ),
            content_sha256=value["content_sha256"],
        )
    except TypeError as error:
        raise ValueError("correction record field type is invalid") from error
    return validate_record(record)
