from dataclasses import replace

from local_kb.correction_match import (
    CorrectionFeatures,
    CorrectionMatcher,
)
from local_kb.correction_model import canonical_correction_hash

from test_correction_model import _record


def _rehash(record):
    blank = replace(record, content_sha256="")
    return replace(blank, content_sha256=canonical_correction_hash(blank))


def _features(**changes):
    values = {
        "space": "work",
        "file_types": frozenset({"xlsx"}),
        "source_families": frozenset({"budget-report"}),
        "sheet_names": frozenset({"年度總表"}),
        "column_names": frozenset({"核准預算"}),
        "units": frozenset({"萬元"}),
        "question_types": frozenset({"amount_lookup"}),
        "keywords": frozenset({"核准", "預算"}),
    }
    values.update(changes)
    return CorrectionFeatures(**values)


def test_matcher_returns_explainable_strong_medium_and_weak_matches():
    matcher = CorrectionMatcher([_record()])

    strong = matcher.match(_features())
    medium = matcher.match(
        _features(
            sheet_names=frozenset(),
            column_names=frozenset(),
            units=frozenset(),
        )
    )
    weak = matcher.match(
        _features(
            file_types=frozenset(),
            source_families=frozenset(),
            sheet_names=frozenset(),
            column_names=frozenset(),
            units=frozenset(),
            question_types=frozenset(),
            keywords=frozenset({"預算"}),
        )
    )

    assert strong.applicable[0]["match_level"] == "strong"
    assert (
        "sheet_names:年度總表"
        in strong.applicable[0]["matched_conditions"]
    )
    assert medium.applicable[0]["match_level"] == "medium"
    assert weak.possible[0]["match_level"] == "weak"


def test_matcher_never_crosses_space_or_activates_inactive_records():
    personal = _rehash(
        replace(
            _record(),
            applicability=replace(
                _record().applicability,
                spaces=("personal",),
            ),
        )
    )
    suspended = _rehash(replace(_record(), status="suspended"))

    result = CorrectionMatcher([personal, suspended]).match(_features())

    assert result.applicable == []
    assert result.possible == []


def test_filename_or_keyword_alone_never_becomes_strong():
    matcher = CorrectionMatcher([_record()])

    result = matcher.match(
        _features(
            source_families=frozenset(),
            sheet_names=frozenset(),
            column_names=frozenset(),
            units=frozenset(),
            question_types=frozenset(),
            keywords=frozenset({"預算"}),
        )
    )

    assert result.applicable == []
    assert result.possible[0]["match_level"] == "weak"
