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


def result_for(keywords: list[str], *, timeline: bool = True) -> TrendsResult:
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
        geo_interest=[
            GeoInterest(
                geo_code="US-CA",
                geo_name="California",
                values=[100],
                has_data=[True],
            )
        ],
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
    assert envelope.completeness_reason == "one or more trend widgets failed"
    assert envelope.data.result.timeline


def test_empty_timeline_without_errors_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def empty(*_args: object, **_kwargs: object) -> object:
        return provider_info(), result_for(["keyword"], timeline=False), []

    monkeypatch.setattr(trends_usecase, "_fetch_trends", empty)

    envelope = trends_usecase.run_trends(Settings(data_dir=tmp_path), "keyword")

    assert envelope.completeness is Completeness.EMPTY
    assert envelope.completeness_reason == "no trend data"
    assert envelope.errors == []


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
