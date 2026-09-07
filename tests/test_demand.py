from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from google_keyword_ai.demand import (
    DemandBatch,
    DemandStatus,
    _coverage,
    combine,
    measured_mean,
    plan_batches,
    select_anchor_by_mean,
)
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
                timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(weeks=i),
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


def test_two_keywords_fit_one_batch() -> None:
    assert plan_batches(["a", "b"], "a") == [["a", "b"]]


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


def test_anchor_keeps_first_usable_batch_coverage() -> None:
    first = series(
        ["anchor", "first"], [[40, 20], [60, 30], [100, 90]], partial=[False, False, True]
    )
    second = series(["anchor", "second"], [[20, 10]])
    rows = combine(
        [
            DemandBatch(keywords=first.keywords, result=first),
            DemandBatch(keywords=second.keywords, result=second),
        ]
    )
    anchors = [row for row in rows if row.is_anchor]
    assert len(anchors) == 1
    anchor = anchors[0]
    assert (anchor.batch, anchor.weeks, anchor.measured_weeks) == (1, 2, 2)
    assert anchor.relative_demand == 100.0
    assert anchor.reason is None
    assert [(row.keyword, row.relative_demand, row.weeks) for row in rows[1:]] == [
        ("first", 50.0, 2),
        ("second", 50.0, 1),
    ]


def test_later_failed_batch_preserves_first_usable_anchor() -> None:
    reason = "Google refused this request: 429"
    first = series(["anchor", "ok"], [[40, 20], [60, 30]])
    rows = combine(
        [
            DemandBatch(keywords=first.keywords, result=first),
            DemandBatch(keywords=["anchor", "lost"], reason=reason),
        ]
    )
    assert [(row.keyword, row.relative_demand, row.batch) for row in rows] == [
        ("anchor", 100.0, 1),
        ("ok", 50.0, 1),
        ("lost", None, 2),
    ]
    assert (rows[0].weeks, rows[0].measured_weeks, rows[0].reason) == (2, 2, None)
    assert rows[-1].reason == reason
    assert (rows[-1].weeks, rows[-1].measured_weeks) == (0, 0)


def test_absent_keyword_or_missing_flags_has_no_measured_mean() -> None:
    result = series(["anchor"], [[20]], [[]])
    assert measured_mean(result, "anchor") is None
    assert measured_mean(result, "absent") is None


def _coverage_pair(
    weeks: int,
    participant_measured: int,
    *,
    anchor_measured: int | None = None,
    participant_value: int = 20,
) -> TrendsResult:
    if anchor_measured is None:
        anchor_measured = weeks
    values: list[list[int]] = []
    flags: list[list[bool]] = []
    for index in range(weeks):
        anchor_has = index < anchor_measured
        participant_has = index < participant_measured
        values.append([10 if anchor_has else 0, participant_value if participant_has else 0])
        flags.append([anchor_has, participant_has])
    return series(["anchor", "thin"], values, flags)


def test_coverage_exactly_at_the_threshold_is_emitted() -> None:
    result = _coverage_pair(4, 1)
    row = next(
        item
        for item in combine(
            [DemandBatch(keywords=result.keywords, result=result)], min_coverage=0.25
        )
        if item.keyword == "thin"
    )
    assert row.relative_demand == 200.0
    assert (row.measured_weeks, row.weeks) == (1, 4)
    assert row.status == DemandStatus.MEASURED
    assert row.reason is None


def test_coverage_just_below_the_threshold_is_cut() -> None:
    result = _coverage_pair(5, 1)
    row = next(
        item
        for item in combine(
            [DemandBatch(keywords=result.keywords, result=result)], min_coverage=0.25
        )
        if item.keyword == "thin"
    )
    assert row.relative_demand is None
    assert row.status == DemandStatus.LOW_COVERAGE
    assert row.reason is not None
    assert "1 of 5 whole weeks" in row.reason
    assert "1" in row.reason
    assert "5" in row.reason
    assert "0.25" in row.reason
    assert "coverage" in row.reason.lower()
    assert "not low demand" in row.reason.lower()


def test_disabled_coverage_threshold_does_not_cut_a_single_week() -> None:
    result = _coverage_pair(53, 1)
    for rows in (
        combine([DemandBatch(keywords=result.keywords, result=result)]),
        combine([DemandBatch(keywords=result.keywords, result=result)], min_coverage=0.0),
    ):
        row = next(item for item in rows if item.keyword == "thin")
        assert row.relative_demand == 200.0
        assert (row.measured_weeks, row.weeks) == (1, 53)
        assert row.status == DemandStatus.MEASURED


def test_coverage_threshold_does_not_overwrite_an_existing_null_reason() -> None:
    result = series(["anchor", "small"], [[50, 0]], [[True, False]])
    row = combine([DemandBatch(keywords=result.keywords, result=result)], min_coverage=0.25)[1]
    assert row.relative_demand is None
    assert row.reason is not None
    assert "below the anchor's resolution" in row.reason
    assert "not zero demand" in row.reason
    assert row.status == DemandStatus.BELOW_RESOLUTION


def test_coverage_threshold_does_not_cut_the_anchor() -> None:
    result = _coverage_pair(53, 1, anchor_measured=1)
    rows = combine([DemandBatch(keywords=result.keywords, result=result)], min_coverage=0.25)
    anchor = next(item for item in rows if item.is_anchor)
    thin = next(item for item in rows if item.keyword == "thin")
    assert anchor.relative_demand == 100.0
    assert anchor.status == DemandStatus.MEASURED
    assert (anchor.measured_weeks, anchor.weeks) == (1, 53)
    assert thin.relative_demand is None
    assert thin.status == DemandStatus.LOW_COVERAGE


def test_zero_weeks_coverage_does_not_divide() -> None:
    failure = None
    coverage = None
    try:
        coverage = _coverage(0, 0)
    except ZeroDivisionError as exc:
        failure = exc
    assert failure is None
    assert coverage == 0.0
    result = series(["anchor", "other"], [[10, 5]], partial=[True])
    rows = combine([DemandBatch(keywords=result.keywords, result=result)], min_coverage=0.25)
    assert all(row.relative_demand is None for row in rows)
    assert all(row.weeks == 0 for row in rows)
    assert all(row.status is DemandStatus.ANCHOR_COLLAPSED for row in rows)


def test_each_demand_status_has_its_own_case() -> None:
    failed_reason = "Google refused this request: 429"
    healthy = series(["anchor", "ok"], [[40, 20], [60, 30]])
    thin = _coverage_pair(5, 1)
    unmeasured = series(["anchor", "small"], [[50, 0]], [[True, False]])
    collapsed = series(["anchor", "ghost"], [[0, 30]], [[False, True]])
    rows = combine(
        [
            DemandBatch(keywords=["anchor", "lost"], reason=failed_reason),
            DemandBatch(keywords=healthy.keywords, result=healthy),
            DemandBatch(keywords=thin.keywords, result=thin),
            DemandBatch(keywords=unmeasured.keywords, result=unmeasured),
            DemandBatch(keywords=collapsed.keywords, result=collapsed),
        ],
        min_coverage=0.25,
    )
    by_key = {row.keyword: row for row in rows}
    assert by_key["anchor"].status == DemandStatus.MEASURED
    assert by_key["anchor"].relative_demand == 100.0
    assert by_key["ok"].status == DemandStatus.MEASURED
    assert by_key["ok"].relative_demand == 50.0
    assert by_key["thin"].status == DemandStatus.LOW_COVERAGE
    assert by_key["thin"].relative_demand is None
    assert by_key["small"].status == DemandStatus.BELOW_RESOLUTION
    assert by_key["small"].relative_demand is None
    assert by_key["small"].reason is not None
    assert "below the anchor's resolution" in by_key["small"].reason
    assert by_key["lost"].status == DemandStatus.BATCH_FAILED
    assert by_key["lost"].reason == failed_reason
    assert by_key["ghost"].status == DemandStatus.ANCHOR_COLLAPSED
    assert by_key["ghost"].relative_demand is None


def test_zero_coverage_prefers_below_resolution_over_low_coverage() -> None:
    result = series(["anchor", "small"], [[50, 0]], [[True, False]])
    row = combine([DemandBatch(keywords=result.keywords, result=result)], min_coverage=0.25)[1]
    assert row.measured_weeks == 0
    assert row.relative_demand is None
    assert row.status == DemandStatus.BELOW_RESOLUTION
    assert row.reason is not None
    assert "below the anchor's resolution" in row.reason
    assert "coverage threshold" not in row.reason


def test_measured_zero_keeps_measured_status() -> None:
    result = series(["anchor", "zero"], [[10, 0]])
    row = combine([DemandBatch(keywords=result.keywords, result=result)], min_coverage=0.25)[1]
    assert row.relative_demand == 0.0
    assert row.status == DemandStatus.MEASURED
    assert row.reason is None


def test_fixture_coverage_passes_the_default_threshold() -> None:
    a = fixture("batch_a.json")
    b = fixture("batch_b.json")
    rows = combine(
        [
            DemandBatch(keywords=["ипотека", "новостройки"], result=a),
            DemandBatch(keywords=["ипотека", "новостройки"], result=b),
        ],
        min_coverage=0.25,
    )
    anchor = next(row for row in rows if row.is_anchor)
    assert anchor.relative_demand == 100.0
    assert all(row.measured_weeks == 53 and row.weeks == 53 for row in rows)
    assert all(
        row.status == DemandStatus.MEASURED for row in rows if row.relative_demand is not None
    )


def test_fixture_coverage_passes_when_threshold_is_disabled() -> None:
    a = fixture("batch_a.json")
    b = fixture("batch_b.json")
    rows = combine(
        [
            DemandBatch(keywords=["ипотека", "новостройки"], result=a),
            DemandBatch(keywords=["ипотека", "новостройки"], result=b),
        ],
        min_coverage=0.0,
    )
    anchor = next(row for row in rows if row.is_anchor)
    assert anchor.relative_demand == 100.0
    assert all(row.measured_weeks == 53 and row.weeks == 53 for row in rows)


def test_demand_status_protocol_literals_are_fixed() -> None:
    assert DemandStatus.MEASURED.value == "measured"
    assert DemandStatus.LOW_COVERAGE.value == "low_coverage"
    assert DemandStatus.BELOW_RESOLUTION.value == "below_resolution"
    assert DemandStatus.ANCHOR_COLLAPSED.value == "anchor_collapsed"
    assert DemandStatus.BATCH_FAILED.value == "batch_failed"
    assert {member.value for member in DemandStatus} == {
        "measured",
        "low_coverage",
        "below_resolution",
        "anchor_collapsed",
        "batch_failed",
    }


def test_thin_measured_zero_is_cut_until_threshold_is_disabled() -> None:
    result = _coverage_pair(5, 1, participant_value=0)
    row = next(
        item
        for item in combine(
            [DemandBatch(keywords=result.keywords, result=result)], min_coverage=0.25
        )
        if item.keyword == "thin"
    )
    assert row.relative_demand is None
    assert row.status == "low_coverage"
    assert row.reason is not None
    assert "1 of 5 whole weeks" in row.reason
    disabled = next(
        item
        for item in combine(
            [DemandBatch(keywords=result.keywords, result=result)], min_coverage=0.0
        )
        if item.keyword == "thin"
    )
    assert disabled.relative_demand == 0.0
    assert disabled.status == "measured"


def test_coverage_fraction_is_not_rounded_before_the_threshold() -> None:
    cut = _coverage_pair(1000, 249)
    row = next(
        item
        for item in combine([DemandBatch(keywords=cut.keywords, result=cut)], min_coverage=0.25)
        if item.keyword == "thin"
    )
    assert row.relative_demand is None
    assert row.status == "low_coverage"
    kept = _coverage_pair(1000, 250)
    kept_row = next(
        item
        for item in combine([DemandBatch(keywords=kept.keywords, result=kept)], min_coverage=0.25)
        if item.keyword == "thin"
    )
    assert kept_row.relative_demand == 200.0
    assert kept_row.status == "measured"


def test_combine_uses_the_min_coverage_argument_not_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GKAI_DEMAND_MIN_COVERAGE", "0.9")
    result = _coverage_pair(53, 1)
    row = next(
        item
        for item in combine(
            [DemandBatch(keywords=result.keywords, result=result)], min_coverage=0.0
        )
        if item.keyword == "thin"
    )
    assert row.relative_demand == 200.0
    assert row.status == "measured"


def test_select_anchor_by_mean_picks_the_highest_mean() -> None:
    result = series(["low", "high", "mid"], [[10, 50, 30], [10, 50, 30]])
    assert select_anchor_by_mean(result, ["low", "high", "mid"]) == "high"
    assert select_anchor_by_mean(result, ["mid", "low"]) == "mid"


def test_select_anchor_by_mean_skips_zero_and_unmeasured() -> None:
    result = series(
        ["zero", "missing", "ok"],
        [[0, 0, 20], [0, 0, 20]],
        [[True, False, True], [True, False, True]],
    )
    assert measured_mean(result, "zero") == 0.0
    assert measured_mean(result, "missing") is None
    assert select_anchor_by_mean(result, ["zero", "missing", "ok"]) == "ok"
    assert select_anchor_by_mean(result, ["zero", "missing"]) is None


def test_select_anchor_by_mean_breaks_ties_by_input_order() -> None:
    result = series(["second", "first"], [[40, 40]])
    assert select_anchor_by_mean(result, ["first", "second"]) == "first"
    assert select_anchor_by_mean(result, ["second", "first"]) == "second"


def test_select_anchor_by_mean_returns_none_when_every_key_is_unusable() -> None:
    result = series(["a", "b"], [[0, 0]], [[False, True]])
    assert select_anchor_by_mean(result, ["a", "b"]) is None
    assert select_anchor_by_mean(result, []) is None


def test_select_anchor_by_mean_ignores_incomplete_weeks() -> None:
    result = series(
        ["complete", "inflated"],
        [[50, 40], [50, 40], [1, 100]],
        partial=[False, False, True],
    )
    assert measured_mean(result, "complete") == 50.0
    assert measured_mean(result, "inflated") == 40.0
    assert select_anchor_by_mean(result, ["complete", "inflated"]) == "complete"


def test_thousandfold_spread_keeps_a_measured_fraction() -> None:
    result = series(
        ["tiny", "giant"],
        [[1 if week == 0 else 0, 100] for week in range(10)],
    )
    tiny_mean = measured_mean(result, "tiny")
    giant_mean = measured_mean(result, "giant")
    assert tiny_mean == 0.1
    assert giant_mean == 100.0
    assert giant_mean / tiny_mean == 1000
    assert select_anchor_by_mean(result, ["tiny", "giant"]) == "giant"
    rows = combine([DemandBatch(keywords=["giant", "tiny"], result=result)])
    by_key = {row.keyword: row for row in rows}
    assert by_key["giant"].relative_demand == 100.0
    assert by_key["giant"].status == DemandStatus.MEASURED
    assert by_key["giant"].is_anchor is True
    assert by_key["tiny"].relative_demand == 0.1
    assert by_key["tiny"].status == DemandStatus.MEASURED
    assert by_key["tiny"].is_anchor is False
