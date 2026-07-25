"""Bounded, provenance-preserving local search helpers."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
import re

from .catalog import Catalog
from .models import SearchHit


MAX_QUESTION_CHARACTERS = 256
MAX_RESULTS = 12
MAX_FALLBACK_ROUTES = 16
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PROJECT_SPACE = re.compile(r"project:[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SPACES = frozenset({"personal", "work", "shared", "unclassified"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{1,159}")


@dataclass(frozen=True)
class EvidenceHit:
    """A raw fragment returned by the catalog, with no generated content."""

    source_id: str
    version_id: str
    space: str
    relative_path: str
    locator: str
    text: str
    score: float
    route: str = "full_question"
    routes: tuple[str, ...] = ()
    coverage: int = 0


def validate_question(question: str) -> str:
    if not isinstance(question, str):
        raise TypeError("question must be a string")
    if len(question) > MAX_QUESTION_CHARACTERS:
        raise ValueError(f"question must be at most {MAX_QUESTION_CHARACTERS} characters")
    if _CONTROL.search(question):
        raise ValueError("question must not contain control characters")
    return question.strip()


def validate_spaces(spaces: Collection[str]) -> tuple[str, ...]:
    if isinstance(spaces, str):
        raise TypeError("spaces must be a collection of strings, not a string")
    try:
        iterator = iter(spaces)
        supplied = []
        for _ in range(17):
            try:
                supplied.append(next(iterator))
            except StopIteration:
                break
    except TypeError as error:
        raise TypeError("spaces must be a collection of strings") from error
    if len(supplied) > 16:
        raise ValueError("too many spaces")
    if not all(isinstance(space, str) for space in supplied):
        raise TypeError("spaces must contain only strings")
    canonical = tuple(sorted(set(supplied)))
    if not canonical:
        raise ValueError("at least one space is required")
    for space in canonical:
        if not isinstance(space, str) or not space or len(space) > 80 or _CONTROL.search(space):
            raise ValueError("space must be a safe non-empty string")
        if space not in _SPACES and _PROJECT_SPACE.fullmatch(space) is None:
            raise ValueError("space is invalid")
    return canonical


def has_searchable_terms(question: str) -> bool:
    return bool(Catalog._plain_query_terms(question))


def ranked_search(
    catalog: Catalog, question: str, spaces: Collection[str], *, limit: int = MAX_RESULTS
) -> list[EvidenceHit]:
    """Return stable raw-fragment results.

    ``Catalog.search`` tokenizes the plain user question itself, creates an AND
    FTS query, and returns ``-bm25`` (larger is better).  In particular, callers
    must not inject the word ``OR``: it would be treated as an ordinary token.
    """
    checked_question = validate_question(question)
    checked_spaces = validate_spaces(spaces)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f"limit must be between 1 and {MAX_RESULTS}")
    if not has_searchable_terms(checked_question):
        return []
    hits = catalog.search(checked_question, checked_spaces, limit=min(40, Catalog.MAX_SEARCH_LIMIT))
    combined: list[EvidenceHit] = [EvidenceHit(
        source_id=hit.source_id, version_id=hit.version_id, space=hit.space,
        relative_path=hit.relative_path, locator=hit.locator, text=hit.text,
        score=float(hit.score) + 1.0, route="full_question",
    ) for hit in hits]
    for route in _fallback_queries(checked_question):
        for hit in catalog.search(route, checked_spaces, limit=8):
            combined.append(EvidenceHit(
                source_id=hit.source_id, version_id=hit.version_id, space=hit.space,
                relative_path=hit.relative_path, locator=hit.locator, text=hit.text,
                # Catalog already exposes -bm25 (larger is better).  A tiny route
                # length bonus makes longer, more specific fallbacks win stable ties.
                score=float(hit.score) + len(route) / 1_000_000, route=f"fallback:{route}",
            ))
    routes = _fallback_queries(checked_question)
    grouped: dict[tuple[str,str], list[EvidenceHit]] = {}
    for hit in combined:
        grouped.setdefault((hit.version_id,hit.locator),[]).append(hit)
    enriched = []
    for candidates in grouped.values():
        hit=max(candidates,key=lambda item:item.score)
        covered = tuple(route for route in routes if route.casefold() in hit.text.casefold())[:16]
        priority=2 if any(item.route=="full_question" for item in candidates) else 1
        specificity=max((len(route) for route in covered),default=0)
        bm25=max(item.score-(1.0 if item.route=="full_question" else 0.0) for item in candidates)
        enriched.append(EvidenceHit(
            source_id=hit.source_id, version_id=hit.version_id, space=hit.space,
            relative_path=hit.relative_path, locator=hit.locator, text=hit.text,
            score=priority*100 + len(covered)*10 + specificity/1000 + bm25/1000000, route="full_question" if priority==2 else hit.route,
            routes=covered, coverage=len(covered),
        ))
    return sorted(enriched, key=lambda hit: (-hit.score, hit.space, hit.source_id, hit.version_id, hit.locator))[:limit]


def exact_routes(
    catalog: Catalog, question: str, spaces: Collection[str], *, limit: int = MAX_RESULTS
) -> list[EvidenceHit]:
    """Find explicit IDs and locations without weakening normal text search."""
    checked_question = validate_question(question)
    checked_spaces = validate_spaces(spaces)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f"limit must be between 1 and {MAX_RESULTS}")
    candidates = _identifier_candidates(checked_question)
    if not candidates:
        return []
    placeholders = ", ".join("?" for _ in checked_spaces)
    sql = f"""
        SELECT sources.source_id, sources.version_id, sources.space,
               sources.relative_path, source_fragments.locator, source_fragments.text
        FROM sources JOIN source_fragments USING (version_id)
        WHERE sources.space IN ({placeholders})
          AND (sources.source_id = ? OR sources.version_id = ?
               OR sources.relative_path = ? OR sources.original_name = ?
               OR source_fragments.locator = ?)
        ORDER BY sources.created_sequence DESC, sources.version_id,
                 source_fragments.locator
        LIMIT ?
    """
    rows = []
    with catalog.connection() as connection:
        for candidate in candidates:
            rows.extend(connection.execute(sql, (*checked_spaces, *(candidate,) * 5, limit)).fetchall())
    return _deduplicate([
        EvidenceHit(
            source_id=row["source_id"], version_id=row["version_id"], space=row["space"],
            relative_path=row["relative_path"], locator=row["locator"], text=row["text"], score=300.0,
            route="exact_identifier",
        )
        for row in rows
    ], limit)


def _identifier_candidates(question: str) -> tuple[str, ...]:
    """Only route strings that look like an intentional identifier or path."""
    candidates = []
    for candidate in _IDENTIFIER.findall(question):
        lowered = candidate.casefold()
        if (any(marker in candidate for marker in "._:/-")
                or lowered.startswith(("src", "ver", "line", "page", "section"))):
            candidates.append(candidate)
    return tuple(dict.fromkeys(candidates))[:16]


def _deduplicate(hits: list[SearchHit] | list[EvidenceHit], limit: int) -> list[EvidenceHit]:
    selected: dict[tuple[str, str], EvidenceHit] = {}
    for hit in hits:
        candidate = hit if isinstance(hit, EvidenceHit) else EvidenceHit(
            source_id=hit.source_id, version_id=hit.version_id, space=hit.space,
            relative_path=hit.relative_path, locator=hit.locator, text=hit.text, score=float(hit.score),
        )
        key = (candidate.version_id, candidate.locator)
        previous = selected.get(key)
        if previous is None or candidate.score > previous.score:
            selected[key] = candidate
    return sorted(
        selected.values(),
        key=lambda hit: (-hit.score, hit.space, hit.source_id, hit.version_id, hit.relative_path, hit.locator),
    )[:limit]


def _fallback_queries(question: str) -> tuple[str, ...]:
    """Generate a small, literal-search fallback set; never FTS ``OR`` syntax."""
    routes: list[str] = []
    stop_words = frozenset({"what", "how", "the", "is", "are", "do", "does", "please", "can", "could", "would", "tell", "me", "about"})
    words = [word for word in Catalog._plain_query_terms(question)
             if Catalog._script_kind(word[0]) is None and len(word) >= 2 and word.casefold() not in stop_words]
    routes.extend(" ".join(words[index:index + 2]) for index in range(max(0, len(words) - 1)))
    routes.extend(words)
    for run in Catalog._script_runs(question):
        # Three-character routes preserve much more precision than single CJK
        # characters; two-character routes cover short meaningful terms.
        for width in (3, 2):
            routes.extend(run[index:index + width] for index in range(max(0, len(run) - width + 1)))
    return tuple(dict.fromkeys(route for route in routes if route))[:MAX_FALLBACK_ROUTES]


def significant_routes(question: str) -> tuple[str, ...]:
    """Bounded searchable fragments shared by retrieval and queue relation checks."""
    return _fallback_queries(question)
