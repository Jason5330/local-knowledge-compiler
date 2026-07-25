"""Validated, deterministic Markdown rendering for knowledge-base wiki pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Iterable


_TYPES = frozenset({"concept", "entity", "topic", "decision", "timeline", "project"})
_SPACES = frozenset({"personal", "work", "shared", "unclassified"})
_STATUSES = frozenset({"active", "disputed", "stale", "archived"})
_CONFIDENCES = frozenset({"high", "medium", "low"})
_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_PROJECT_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HEADINGS = frozenset({
    "Current State", "Evidence", "Conflicts and Gaps", "Related", "Timeline",
})


@dataclass(frozen=True)
class WikiPage:
    # Keep this original field order: other tasks construct pages positionally.
    page_id: str
    title: str
    page_type: str
    space: str
    confidence: str
    source_ids: tuple[str, ...]
    current_state: str
    conflicts: tuple[str, ...]
    timeline_entry: str
    aliases: tuple[str, ...] = ()
    status: str = "active"
    updated_at: str = ""
    related: tuple[str, ...] = ()


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _as_strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{label} must be a tuple")
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must contain strings")
    return value


def _validate_line_safe(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or _has_control(value):
        raise ValueError(f"{label} must be a non-empty control-character-free string")


def _validate_body(value: str, label: str) -> None:
    _validate_line_safe(value, label)
    for line in value.splitlines():
        if line.strip() in {f"## {heading}" for heading in _HEADINGS}:
            raise ValueError(f"{label} may not inject a reserved section heading")


def _validate_timestamp(value: str) -> None:
    _validate_line_safe(value, "updated_at")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("updated_at must be ISO-8601") from exc


def validate_page(page: WikiPage) -> None:
    """Validate a page before it can be rendered or committed to a vault."""
    if not isinstance(page, WikiPage):
        raise ValueError("page must be a WikiPage")

    # Source validation intentionally comes first: it is provenance's primary invariant.
    source_ids = _as_strings(page.source_ids, "source_ids")
    if not source_ids:
        raise ValueError("source_ids must contain at least one source")
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("source_ids must be unique")
    if any(not _SOURCE_ID.fullmatch(source_id) for source_id in source_ids):
        raise ValueError("source_ids must be canonical safe strings")

    for value, label in ((page.page_id, "page_id"), (page.title, "title")):
        _validate_line_safe(value, label)
    if page.page_type not in _TYPES:
        raise ValueError("page_type is invalid")
    if page.space not in _SPACES:
        if not page.space.startswith("project:") or not _PROJECT_SLUG.fullmatch(page.space[8:]):
            raise ValueError("space is invalid")
    if page.confidence not in _CONFIDENCES:
        raise ValueError("confidence is invalid")
    if page.status not in _STATUSES:
        raise ValueError("status is invalid")
    _validate_timestamp(page.updated_at)

    aliases = _as_strings(page.aliases, "aliases")
    related = _as_strings(page.related, "related")
    conflicts = _as_strings(page.conflicts, "conflicts")
    for group, label in ((aliases, "aliases"), (related, "related"), (conflicts, "conflicts")):
        if len(set(group)) != len(group):
            raise ValueError(f"{label} must be unique")
        for item in group:
            _validate_line_safe(item, label)
    _validate_body(page.current_state, "current_state")
    _validate_body(page.timeline_entry, "timeline_entry")


def _yaml_scalar(value: str) -> str:
    """JSON scalar syntax is also safe, portable YAML scalar syntax."""
    return json.dumps(value, ensure_ascii=False)


def _yaml_list(key: str, values: Iterable[str]) -> list[str]:
    values = tuple(values)
    if not values:
        return [f"{key}: []"]
    return [f"{key}:", *(f"  - {_yaml_scalar(value)}" for value in values)]


def _list_or_none(values: tuple[str, ...]) -> str:
    return "\n".join(f"- {value}" for value in values) if values else "無"


def render_page(page: WikiPage) -> str:
    """Render a validated page with fixed, non-injectable Markdown sections."""
    validate_page(page)
    frontmatter = [
        "---",
        f"id: {_yaml_scalar(page.page_id)}",
        f"title: {_yaml_scalar(page.title)}",
        *_yaml_list("aliases", page.aliases),
        f"type: {_yaml_scalar(page.page_type)}",
        f"space: {_yaml_scalar(page.space)}",
        f"status: {_yaml_scalar(page.status)}",
        f"confidence: {_yaml_scalar(page.confidence)}",
        f"updated_at: {_yaml_scalar(page.updated_at)}",
        *_yaml_list("source_ids", page.source_ids),
        "---",
    ]
    sections = [
        "## Current State", page.current_state,
        "## Evidence", _list_or_none(page.source_ids),
        "## Conflicts and Gaps", _list_or_none(page.conflicts),
        "## Related", _list_or_none(page.related),
        "## Timeline", page.timeline_entry,
    ]
    return "\n".join(frontmatter) + "\n\n" + "\n\n".join(sections) + "\n"
