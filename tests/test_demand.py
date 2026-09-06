from datetime import UTC, datetime
from pathlib import Path

import pytest

from google_keyword_ai.demand import DemandBatch, combine, measured_mean, plan_batches
from google_keyword_ai.errors import InvalidConfigurationError
from google_keyword_ai.providers.trends.models import TrendPoint, TrendsResult

FIXTURES = Path(__file__).parent / "fixtures" / "demand"


def fixture(name: str) -> TrendsResult:
    return TrendsResult.model_validate_json((FIXTURES / name).read_bytes())


def series(
    keywords: list[str],
    values: list[list[int]],
    measured: list[list[bool]] | None = None,
    partial: list[bool] | None = None,
) -> TrendsResult:
    return TrendsResult(
        keywords=keywords,
        geo="RU",
        timeframe="today 12-m",
        normalization_scope="synthetic",
        timeline=[
            TrendPoint(
                timestamp=datetime(2026, 1, i + 1, tzinfo=UTC),
                formatted_time=str(i),
                values=row,
                has_data=measured[i] if measured is not None else [True] * len(row),
                is_partial=partial[i] if partial is not None else False,
            )
            for i, row in enumerate(values)
        ],
        retrieved_at=datetime(2026, 9, 6, tzinfo=UTC),
        source="fixture",
    )


def test_real_batches_stitch_within_half_a_percent() -> None:
    a = fixture("batch_a.json")
    b = fixture("batch_b.json")
    rows = combine(
        [
            DemandBatch(keywords=["ипотека", "новостройки"], result=a),
            DemandBatch(keywords=["ипотека", "новостройки"], result=b),
        ]
    )
    control = [row.relative_demand for row in rows if row.keyword == "новостройки"]
    assert len(control) == 2
    first, second = control
    assert first is not None and second is not None
    assert first == pytest.approx(28.17680, abs=0.00001)
    assert second == pytest.approx(28.12500, abs=0.00001)
    assert abs(first - second) / first * 100 < 0.5
    assert sum(row.is_anchor for row in rows) == 1


def test_anchor_is_found_by_name_at_index_two() -> None:
    result = fixture("batch_a.json")
    assert result.keywords[2] == "ипотека"
    rows = combine([DemandBatch(keywords=["ипотека", "новостройки"], result=result)])
    participant = next(row for row in rows if not row.is_anchor)
    assert participant.relative_demand == pytest.approx(28.17680, abs=0.00001)


def test_batch_uses_anchor_mean_instead_of_batch_maximum() -> None:
    result = series(["anchor", "larger"], [[20, 100], [60, 60]])
    rows = combine([DemandBatch(keywords=result.keywords, result=result)])
    assert rows[0].relative_demand == 200.0
    assert rows[0].keyword == "larger"
    assert rows[1].relative_demand == 100.0


def test_unmeasured_participant_is_unknown_not_zero() -> None:
    result = series(["anchor", "small"], [[50, 0]], [[True, False]])
    row = combine([DemandBatch(keywords=result.keywords, result=result)])[1]
    assert row.relative_demand is None
    assert row.measured_weeks == 0
    assert row.weeks == 1
    assert row.reason is not None
    assert "below the anchor's resolution" in row.reason
    assert "not zero demand" in row.reason


def test_anchor_collapse_invalidates_the_whole_batch() -> None:
    result = series(["anchor", "small"], [[0, 30]], [[False, True]])
    rows = combine([DemandBatch(keywords=result.keywords, result=result)])
    assert all(row.relative_demand is None for row in rows)
    assert all(row.reason and "anchor" in row.reason.lower() for row in rows)
    assert rows[1].measured_weeks == 1


def test_measured_zero_anchor_also_collapses() -> None:
    result = series(["anchor", "small"], [[0, 30]])
    rows = combine([DemandBatch(keywords=result.keywords, result=result)])
    assert all(row.relative_demand is None for row in rows)


def test_mean_uses_only_measured_whole_weeks() -> None:
    result = series(
        ["anchor", "small"],
        [[10, 20], [40, 0], [90, 100]],
        [[True, True], [True, False], [True, True]],
        [False, False, True],
    )
    assert measured_mean(result, "small") == 20.0
    assert measured_mean(result, "anchor") == 25.0
    row = next(
        row
        for row in combine([DemandBatch(keywords=result.keywords, result=result)])
        if row.keyword == "small"
    )
    assert row.relative_demand == 80.0
    assert (row.measured_weeks, row.weeks) == (1, 2)


def test_measured_zero_is_a_number() -> None:
    result = series(["anchor", "zero"], [[10, 0]])
    row = combine([DemandBatch(keywords=result.keywords, result=result)])[1]
    assert row.relative_demand == 0.0
    assert row.reason is None


def test_fifty_keywords_fit_thirteen_batches() -> None:
    batches = plan_batches([f"key{i}" for i in range(50)], "key0")
    assert len(batches) == 13
    assert batches[0] == ["key0", "key1", "key2", "key3", "key4"]
    assert batches[-1] == ["key0", "key49"]
    assert [key for batch in batches for key in batch[1:]] == [f"key{i}" for i in range(1, 50)]


def test_fifty_one_keywords_are_refused() -> None:
    reason = None
    try:
        plan_batches([f"key{i}" for i in range(51)], "key0")
    except InvalidConfigurationError as exc:
        reason = str(exc)
    assert reason is not None
    assert "50" in reason


def test_nulls_sort_last_in_input_order_even_beside_measured_zero() -> None:
    result = series(
        ["anchor", "unknown1", "zero", "unknown2"], [[10, 0, 0, 0]], [[True, False, True, False]]
    )
    rows = combine([DemandBatch(keywords=result.keywords, result=result)])
    assert [row.keyword for row in rows] == ["anchor", "zero", "unknown1", "unknown2"]


def test_batch_plan_normalizes_and_keeps_first_occurrence_order() -> None:
    assert plan_batches([" B  ", "\uff21", "b", "C", " a ", "D", "E", "F"], " A ") == [
        ["a", "b", "c", "d", "e"],
        ["a", "f"],
    ]


@pytest.mark.parametrize(
    "keywords, anchor", [(["a"], "a"), (["A", " a "], "a"), (["a", "b"], "c"), (["a", " "], "a")]
)
def test_invalid_plan_is_refused(keywords: list[str], anchor: str) -> None:
    with pytest.raises(InvalidConfigurationError):
        plan_batches(keywords, anchor)


def test_failed_batch_reason_is_verbatim_and_later_anchor_can_recover() -> None:
    reason = "Google refused this request: 429"
    result = series(["anchor", "ok"], [[40, 20]])
    rows = combine(
        [
            DemandBatch(keywords=["anchor", "lost"], reason=reason),
            DemandBatch(keywords=result.keywords, result=result),
        ]
    )
    assert [(r.keyword, r.relative_demand, r.batch) for r in rows] == [
        ("anchor", 100.0, 2),
        ("ok", 50.0, 2),
        ("lost", None, 1),
    ]
    assert rows[-1].reason == reason
    assert (rows[-1].measured_weeks, rows[-1].weeks) == (0, 0)


def test_absent_keyword_or_missing_flags_has_no_measured_mean() -> None:
    result = series(["anchor"], [[20]], [[]])
    assert measured_mean(result, "anchor") is None
    assert measured_mean(result, "absent") is None
