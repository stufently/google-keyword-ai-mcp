import math
from datetime import UTC, datetime, timedelta

import pytest

from google_keyword_ai.config import Settings
from google_keyword_ai.pipeline.models import ResearchKeyword
from google_keyword_ai.providers.trends.models import TrendPoint, TrendsResult
from google_keyword_ai.scoring import compute_trend_growth, score_keyword


def keyword(**values: object) -> ResearchKeyword:
    return ResearchKeyword.model_validate(
        {"keyword": "alpha", "normalized": "alpha", "discovered_from": [], **values}
    )


def component(result: object, name: str) -> object:
    return next(item for item in result.components if item.name == name)  # type: ignore[attr-defined]


def test_demand_formula() -> None:
    settings = Settings(score_demand_reference=100_000)
    result = score_keyword(keyword(avg_monthly_searches=1_000), settings)
    expected = 100 * math.log10(1_001) / math.log10(100_001)
    assert component(result, "demand").normalized == pytest.approx(expected)  # type: ignore[attr-defined]


def test_trend_formula() -> None:
    result = score_keyword(keyword(), Settings(), trend_growth=0.4)
    assert component(result, "trend").normalized == pytest.approx(70.0)  # type: ignore[attr-defined]


def test_commercial_formula() -> None:
    result = score_keyword(keyword(high_top_of_page_bid=2.5), Settings(score_bid_reference=5))
    assert component(result, "commercial").normalized == pytest.approx(50.0)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        # Position 30 sits at the very top of the window, where the position
        # factor is exactly 1.0 — testing only there would pass even if the
        # factor were dropped from the formula entirely.
        pytest.param(30.0, 50.0, id="opportunity_at_window_top"),
        pytest.param(15.5, 25.0, id="opportunity_mid_window"),
        pytest.param(1.0, 0.0, id="opportunity_at_first_place"),
    ],
)
def test_opportunity_formula(position: float, expected: float) -> None:
    result = score_keyword(keyword(gsc_impressions=500, gsc_position=position), Settings())
    assert component(result, "opportunity").normalized == pytest.approx(expected)  # type: ignore[attr-defined]


def test_missing_components_are_excluded_from_average() -> None:
    result = score_keyword(keyword(avg_monthly_searches=1_000), Settings())
    demand = component(result, "demand")
    assert result.score == pytest.approx(demand.normalized)  # type: ignore[attr-defined]


def test_none_available_returns_zero_and_none_confidence() -> None:
    result = score_keyword(keyword(), Settings())
    assert result.score == 0.0
    assert result.confidence == "none"


@pytest.mark.parametrize(
    ("values", "growth", "expected"),
    [
        ({}, None, "none"),
        ({"avg_monthly_searches": 10}, None, "low"),
        ({"avg_monthly_searches": 10, "high_top_of_page_bid": 1}, None, "low"),
        ({"avg_monthly_searches": 10, "high_top_of_page_bid": 1}, 0.0, "medium"),
        (
            {
                "avg_monthly_searches": 10,
                "high_top_of_page_bid": 1,
                "gsc_impressions": 10,
                "gsc_position": 10,
            },
            0.0,
            "high",
        ),
    ],
)
def test_confidence_tracks_available_components(
    values: dict[str, object], growth: float | None, expected: str
) -> None:
    assert score_keyword(keyword(**values), Settings(), trend_growth=growth).confidence == expected


def trends(values: list[int]) -> TrendsResult:
    now = datetime.now(UTC)
    return TrendsResult(
        keywords=["alpha"],
        geo="US",
        timeframe="today 12-m",
        normalization_scope="one-scope",
        timeline=[
            TrendPoint(timestamp=now + timedelta(days=index), formatted_time="", values=[value])
            for index, value in enumerate(values)
        ],
        retrieved_at=now,
        source="google_trends",
    )


def test_growth_is_none_for_short_timeline() -> None:
    assert compute_trend_growth(trends([1] * 7)) is None


def test_growth_compares_last_two_quarters() -> None:
    assert compute_trend_growth(trends([1, 1, 10, 10, 20, 20, 40, 40])) == pytest.approx(1.0)
