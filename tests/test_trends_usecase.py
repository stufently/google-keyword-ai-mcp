from datetime import UTC, datetime
from pathlib import Path

import pytest

from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness
from google_keyword_ai.errors import InvalidConfigurationError, ProviderUnavailableError
from google_keyword_ai.providers.base import ProviderInfo
from google_keyword_ai.providers.trends.models import (
    GeoInterest,
    TrendPoint,
    TrendsResult,
    build_normalization_scope,
)
from google_keyword_ai.providers.trends.provider import GoogleTrendsProvider
from google_keyword_ai.usecases import trends as trends_usecase


def result_for(keywords: list[str], *, timeline: bool = True, geo: bool = True) -> TrendsResult:
    return TrendsResult(
        keywords=keywords,
        geo="US",
        timeframe="today 12-m",
        normalization_scope=build_normalization_scope(
            keywords,
            geo="US",
            timeframe="today 12-m",
            hl="en",
        ),
        timeline=(
            [
                TrendPoint(
                    timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                    formatted_time="Jan 2026",
                    values=[50],
                    has_data=[True],
                )
            ]
            if timeline
            else []
        ),
        geo_interest=(
            [
                GeoInterest(
                    geo_code="US-CA",
                    geo_name="California",
                    values=[100],
                    has_data=[True],
                )
            ]
            if geo
            else []
        ),
        retrieved_at=datetime(2026, 9, 2, tzinfo=UTC),
        source="https://trends.google.com/trends/api/explore",
    )


def provider_info() -> ProviderInfo:
    return ProviderInfo(name="trends", official=False, stability="unofficial")


def test_provider_failure_returns_empty_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*_args: object, **_kwargs: object) -> object:
        raise ProviderUnavailableError("trends unavailable")

    monkeypatch.setattr(trends_usecase, "_fetch_trends", fail)

    envelope = trends_usecase.run_trends(Settings(data_dir=tmp_path), "keyword")

    assert envelope.completeness is Completeness.EMPTY
    assert envelope.errors == ["trends unavailable"]
    assert envelope.completeness_reason == "trends unavailable"
    assert envelope.data.result.timeline == []


def test_partial_widget_result_has_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def partial(*_args: object, **_kwargs: object) -> object:
        return provider_info(), result_for(["keyword"]), ["GEO_MAP: failed"]

    monkeypatch.setattr(trends_usecase, "_fetch_trends", partial)

    envelope = trends_usecase.run_trends(Settings(data_dir=tmp_path), "keyword")

    assert envelope.completeness is Completeness.PARTIAL
    assert envelope.warnings == ["GEO_MAP: failed"]
    assert envelope.completeness_reason == "GEO_MAP: failed"
    assert envelope.data.result.timeline


def test_a_reply_with_nothing_in_it_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def empty(*_args: object, **_kwargs: object) -> object:
        return provider_info(), result_for(["keyword"], timeline=False, geo=False), []

    monkeypatch.setattr(trends_usecase, "_fetch_trends", empty)

    envelope = trends_usecase.run_trends(Settings(data_dir=tmp_path), "keyword")

    assert envelope.completeness is Completeness.EMPTY
    assert envelope.completeness_reason == "no trend data"
    assert envelope.errors == []


def test_geography_without_a_timeline_is_still_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`empty` over a payload with rows in it is a false statement about the payload.

    The timeline is the headline of a Trends answer, but geography and related
    queries are answers too, and Google returns them for keywords whose timeline
    is empty. Reading emptiness off the timeline alone told the caller there was
    nothing while handing them a region list.
    """

    async def geo_only(*_args: object, **_kwargs: object) -> object:
        return provider_info(), result_for(["keyword"], timeline=False), []

    monkeypatch.setattr(trends_usecase, "_fetch_trends", geo_only)

    envelope = trends_usecase.run_trends(Settings(data_dir=tmp_path), "keyword")

    assert envelope.data.result.geo_interest, "the fixture is only useful if it has rows"
    assert envelope.completeness is Completeness.COMPLETE
    assert envelope.completeness_reason is None


def test_a_total_widget_outage_is_empty_and_says_which_widget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing came back, so `partial` would promise data the payload lacks.

    `partial` was reported for any warning at all, including the case where
    every widget failed and the result held nothing. And the reason has to be
    the widget failure, not the flat "no trend data" -- that reads as Google's
    verdict on the keyword rather than as a request that never landed.
    """

    async def outage(*_args: object, **_kwargs: object) -> object:
        return (
            provider_info(),
            result_for(["keyword"], timeline=False, geo=False),
            ["TIMESERIES: rate limited", "GEO_MAP: rate limited"],
        )

    monkeypatch.setattr(trends_usecase, "_fetch_trends", outage)

    envelope = trends_usecase.run_trends(Settings(data_dir=tmp_path), "keyword")

    assert envelope.completeness is Completeness.EMPTY
    assert envelope.completeness_reason == "TIMESERIES: rate limited"
    assert envelope.warnings == ["TIMESERIES: rate limited", "GEO_MAP: rate limited"]


def test_normalization_scope_is_stable_and_query_specific() -> None:
    first = build_normalization_scope(["one", "two"], geo="US", timeframe="today 12-m", hl="en")
    same = build_normalization_scope(["one", "two"], geo="US", timeframe="today 12-m", hl="en")
    different = build_normalization_scope(
        ["one", "three"], geo="US", timeframe="today 12-m", hl="en"
    )

    assert first == same
    assert first != different
    assert len(first) == 16


def test_run_trends_compare_sends_all_keywords_in_one_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    async def capture(
        _settings: Settings,
        _cache: object,
        keywords: list[str],
        *_args: object,
    ) -> object:
        calls.append(list(keywords))
        return provider_info(), result_for(keywords), []

    monkeypatch.setattr(trends_usecase, "_fetch_trends", capture)

    envelope = trends_usecase.run_trends_compare(
        Settings(data_dir=tmp_path), ["one", "two", "three"]
    )

    assert envelope.completeness is Completeness.COMPLETE
    assert calls == [["one", "two", "three"]]
    assert envelope.data.result.keywords == ["one", "two", "three"]


@pytest.mark.parametrize("keywords", [[], ["1", "2", "3", "4", "5", "6"]])
def test_run_trends_compare_validates_keyword_count(tmp_path: Path, keywords: list[str]) -> None:
    with pytest.raises(InvalidConfigurationError):
        trends_usecase.run_trends_compare(Settings(data_dir=tmp_path), keywords)


def test_kill_switch_disables_provider_and_returns_empty(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, trends_enabled=False)

    assert GoogleTrendsProvider(settings=settings).is_available() is False
    envelope = trends_usecase.run_trends(settings, "keyword")

    assert envelope.completeness is Completeness.EMPTY
    assert envelope.errors == ["Google Trends is disabled by configuration."]


def test_a_comparison_is_partial_without_claiming_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not every warning is a failure, and the reason must not say it was one.

    Google splits related queries one per keyword in a comparison. Related
    queries really are missing from the payload, so `partial` is right — but a
    fixed sentence about a failed widget tells the caller something went wrong
    on a request where nothing did.
    """
    split = (
        "RELATED_QUERIES came back once per keyword (RELATED_QUERIES_0, "
        "RELATED_QUERIES_1); a comparison normalises each separately, so they "
        "are not merged into one list and are not reported here."
    )

    async def comparison(*_args: object, **_kwargs: object) -> object:
        return provider_info(), result_for(["one", "two"]), [split]

    monkeypatch.setattr(trends_usecase, "_fetch_trends", comparison)

    envelope = trends_usecase.run_trends_compare(Settings(data_dir=tmp_path), ["one", "two"])

    assert envelope.completeness is Completeness.PARTIAL
    assert envelope.completeness_reason == split
    assert "failed" not in (envelope.completeness_reason or "")
