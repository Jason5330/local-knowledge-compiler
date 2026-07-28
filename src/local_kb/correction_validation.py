"""Mechanical validation for automatic correction triggers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

from .query import evidence_sha256


SUPPORTED_CHECKS = frozenset(
    {"citation_identity", "decimal_relation", "unit_scale"}
)
UNIT_SCALE = {
    ("元", "萬元"): Decimal("0.0001"),
    ("萬元", "元"): Decimal("10000"),
    ("公斤", "噸"): Decimal("0.001"),
    ("噸", "公斤"): Decimal("1000"),
}
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


def _decimal(value: object, label: str) -> Decimal:
    if (
        not isinstance(value, str)
        or len(value) > 200
        or _DECIMAL.fullmatch(value) is None
    ):
        raise ValueError(f"{label} must be an exact decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{label} is invalid") from error
    if not result.is_finite() or abs(result.as_tuple().exponent) > 12:
        raise ValueError(f"{label} exceeds decimal limits")
    return result


def _validate_decimal_relation(
    validation: dict[str, object],
) -> dict[str, object]:
    operands = validation.get("operands")
    operator = validation.get("operator")
    if (
        not isinstance(operands, list)
        or not operands
        or len(operands) > 100
        or operator not in {"sum", "difference", "product"}
    ):
        raise ValueError("deterministic decimal relation is invalid")
    numbers = [_decimal(item, "operand") for item in operands]
    claimed = _decimal(validation.get("claimed_result"), "claimed_result")
    if operator == "sum":
        computed = sum(numbers, Decimal(0))
    elif operator == "difference":
        computed = numbers[0]
        for number in numbers[1:]:
            computed -= number
    else:
        computed = Decimal(1)
        for number in numbers:
            computed *= number
    if claimed == computed:
        raise ValueError("deterministic validation found no error")
    return {
        "kind": "decimal_relation",
        "verified": True,
        "computed_result": str(computed),
    }


def _validate_unit_scale(
    validation: dict[str, object],
) -> dict[str, object]:
    value = _decimal(validation.get("value"), "value")
    claimed = _decimal(validation.get("claimed_result"), "claimed_result")
    source_unit = validation.get("source_unit")
    target_unit = validation.get("target_unit")
    factor = UNIT_SCALE.get((source_unit, target_unit))
    if factor is None:
        raise ValueError("deterministic unit scale is unsupported")
    computed = value * factor
    if claimed == computed:
        raise ValueError("deterministic validation found no error")
    return {
        "kind": "unit_scale",
        "verified": True,
        "computed_result": str(computed),
    }


def _raw_identity(item: object) -> tuple[object, ...] | None:
    if not isinstance(item, dict) or item.get("kind") != "raw_fragment":
        return None
    try:
        supplied = item["evidence_sha256"]
        if not isinstance(supplied, str) or supplied != evidence_sha256(item):
            return None
        return (
            item["source_id"],
            item["version_id"],
            item["locator"],
            supplied,
        )
    except KeyError:
        return None


def _validate_citation_identity(
    packet: dict[str, object],
    validation: dict[str, object],
) -> dict[str, object]:
    citation = validation.get("citation")
    if not isinstance(citation, dict):
        raise ValueError("deterministic citation identity is invalid")
    keys = ("source_id", "version_id", "locator", "evidence_sha256")
    if set(citation) != set(keys):
        raise ValueError("deterministic citation identity is invalid")
    claimed = tuple(citation[key] for key in keys)
    evidence = packet.get("evidence")
    if not isinstance(evidence, list):
        raise ValueError("packet evidence is invalid")
    allowed = {
        identity
        for item in evidence
        if (identity := _raw_identity(item)) is not None
    }
    if claimed in allowed:
        raise ValueError("deterministic validation found no citation error")
    return {
        "kind": "citation_identity",
        "verified": True,
    }


def validate_trigger(
    packet: dict[str, object],
    proposal: dict[str, object],
) -> dict[str, object]:
    trigger = proposal.get("trigger_type")
    if trigger == "user_reported_wrong":
        report = proposal.get("user_report")
        if (
            not isinstance(report, str)
            or not report.strip()
            or len(report) > 2_000
        ):
            raise ValueError(
                "user-reported correction requires a bounded user report"
            )
        return {"kind": "user_report", "verified": True}
    if trigger != "deterministic_validation_failure":
        raise ValueError("invalid correction trigger")
    validation = proposal.get("validation")
    if (
        not isinstance(validation, dict)
        or validation.get("kind") not in SUPPORTED_CHECKS
    ):
        raise ValueError(
            "deterministic correction requires a supported validation"
        )
    if validation["kind"] == "citation_identity":
        return _validate_citation_identity(packet, validation)
    if validation["kind"] == "decimal_relation":
        return _validate_decimal_relation(validation)
    return _validate_unit_scale(validation)
