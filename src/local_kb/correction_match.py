"""Explainable, deterministic matching for local correction records."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re

from .catalog import Catalog
from .correction_model import CorrectionRecord


@dataclass(frozen=True)
class CorrectionFeatures:
    space: str
    file_types: frozenset[str]
    source_families: frozenset[str]
    sheet_names: frozenset[str]
    column_names: frozenset[str]
    units: frozenset[str]
    question_types: frozenset[str]
    keywords: frozenset[str]


@dataclass(frozen=True)
class MatchResult:
    applicable: list[dict[str, object]]
    possible: list[dict[str, object]]
    total_considered: int
    truncated: bool


def _normalized(values) -> dict[str, str]:
    return {
        value.strip().casefold(): value
        for value in values
        if isinstance(value, str) and value.strip()
    }


def _matches(
    label: str,
    configured,
    observed,
) -> tuple[list[str], list[str]]:
    expected = _normalized(configured)
    actual = set(_normalized(observed))
    matched = [
        f"{label}:{expected[key]}"
        for key in sorted(expected)
        if key in actual
    ]
    missing = [
        f"{label}:{expected[key]}"
        for key in sorted(expected)
        if key not in actual
    ]
    return matched, missing


def _public_match(
    record: CorrectionRecord,
    level: str,
    matched: list[str],
    unmatched: list[str],
) -> dict[str, object]:
    return {
        "correction_id": record.correction_id,
        "match_level": level,
        "matched_conditions": matched,
        "unmatched_conditions": unmatched,
        "reason": (
            f"{level} match: " + ", ".join(matched)
            if matched
            else f"{level} match"
        ),
        "content_sha256": record.content_sha256,
        "rule": record.correction_rule,
        "supporting_evidence": [
            asdict(reference)
            for reference in record.supporting_evidence
        ],
        "verification_required": level == "medium",
    }


def _match_records(
    records: list[CorrectionRecord],
    features: CorrectionFeatures,
    *,
    max_applicable: int,
    max_possible: int,
) -> MatchResult:
    applicable = []
    possible = []
    considered = 0
    truncated = False
    for record in sorted(records, key=lambda item: item.correction_id):
        if (
            record.status != "active"
            or record.applicability.spaces != (features.space,)
        ):
            continue
        considered += 1
        groups = {}
        missing = {}
        for label, configured, observed in (
            (
                "file_types",
                record.applicability.file_types,
                features.file_types,
            ),
            (
                "source_families",
                record.applicability.source_families,
                features.source_families,
            ),
            (
                "sheet_names",
                record.applicability.sheet_names,
                features.sheet_names,
            ),
            (
                "column_names",
                record.applicability.column_names,
                features.column_names,
            ),
            ("units", record.applicability.units, features.units),
            (
                "question_types",
                record.applicability.question_types,
                features.question_types,
            ),
            (
                "keywords",
                record.applicability.keywords,
                features.keywords,
            ),
        ):
            groups[label], missing[label] = _matches(
                label,
                configured,
                observed,
            )
        structural = sum(
            bool(groups[label])
            for label in (
                "source_families",
                "sheet_names",
                "column_names",
                "units",
            )
        )
        supporting = sum(
            bool(groups[label])
            for label in ("file_types", "question_types", "keywords")
        )
        if structural >= 2 and supporting >= 1:
            level = "strong"
        elif structural >= 1 and supporting >= 2:
            level = "medium"
        elif groups["keywords"]:
            level = "weak"
        else:
            continue
        matched = [
            item
            for label in groups
            for item in groups[label]
        ]
        unmatched = [
            item
            for label in missing
            for item in missing[label]
        ]
        public = _public_match(
            record,
            level,
            matched,
            unmatched,
        )
        if level == "weak":
            if len(possible) < max_possible:
                possible.append(public)
            else:
                truncated = True
        elif len(applicable) < max_applicable:
            applicable.append(public)
        else:
            truncated = True
    return MatchResult(
        applicable=applicable,
        possible=possible,
        total_considered=considered,
        truncated=truncated,
    )


class CorrectionMatcher:
    MAX_APPLICABLE = 20
    MAX_POSSIBLE = 10

    def __init__(self, records: list[CorrectionRecord]) -> None:
        self.records = records

    def match(self, features: CorrectionFeatures) -> MatchResult:
        return _match_records(
            self.records,
            features,
            max_applicable=self.MAX_APPLICABLE,
            max_possible=self.MAX_POSSIBLE,
        )


def normalize_source_family(original_name: str) -> str:
    stem = Path(original_name).stem.casefold()
    stem = re.sub(
        r"(?:^|[-_\s])(?:19|20)\d{2}(?:[-_]?\d{1,2}){0,2}(?=$|[-_\s])",
        "-",
        stem,
    )
    stem = re.sub(r"[-_\s]+", "-", stem).strip("-")
    return stem[:200]


def features_from_packet_inputs(
    catalog: Catalog,
    question: str,
    space: str,
    evidence: list[dict[str, object]],
) -> CorrectionFeatures:
    version_ids = [
        str(item["version_id"])
        for item in evidence
        if item.get("kind") == "raw_fragment"
        and isinstance(item.get("version_id"), str)
    ]
    metadata = catalog.source_metadata(version_ids)
    file_types = set()
    source_families = set()
    sheets = set()
    bounded_text = question[:16_000]
    for item in evidence[:100]:
        if item.get("kind") != "raw_fragment":
            continue
        version_id = item.get("version_id")
        source = metadata.get(str(version_id), {})
        original_name = source.get("original_name")
        if isinstance(original_name, str):
            suffix = Path(original_name).suffix.casefold().lstrip(".")
            if suffix:
                file_types.add(suffix)
            family = normalize_source_family(original_name)
            if family:
                source_families.add(family)
        locator = item.get("locator")
        if isinstance(locator, str) and locator.startswith("sheet:"):
            sheet = locator[6:].split(";", 1)[0].strip()
            if sheet:
                sheets.add(sheet)
        text = item.get("text")
        if isinstance(text, str):
            bounded_text += "\n" + text[:8_000]
    lowered = bounded_text.casefold()
    significant = set(Catalog._plain_query_terms(question))
    question_types = set()
    if any(marker in lowered for marker in ("多少", "金額", "amount")):
        question_types.add("amount_lookup")
    if any(marker in lowered for marker in ("加總", "總和", "sum")):
        question_types.add("sum")
    if any(marker in lowered for marker in ("比較", "哪個較", "compare")):
        question_types.add("comparison")
    if any(marker in lowered for marker in ("最新", "最新版", "latest")):
        question_types.add("latest_version")
    if any(marker in lowered for marker in ("差異", "相差", "difference")):
        question_types.add("difference")
    known_units = {"元", "萬元", "公斤", "噸", "kg", "t"}
    units = {unit for unit in known_units if unit.casefold() in lowered}
    return CorrectionFeatures(
        space=space,
        file_types=frozenset(file_types),
        source_families=frozenset(source_families),
        sheet_names=frozenset(sheets),
        column_names=frozenset(
            term
            for term in significant
            if term.casefold() in lowered
        ),
        units=frozenset(units),
        question_types=frozenset(question_types),
        keywords=frozenset(significant),
    )
