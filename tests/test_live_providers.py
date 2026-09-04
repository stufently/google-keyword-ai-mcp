import unicodedata
from pathlib import Path

import pytest

from google_keyword_ai.cache import SqliteCache
from google_keyword_ai.config import Settings
from google_keyword_ai.http import build_client
from google_keyword_ai.market import Market
from google_keyword_ai.providers.autocomplete import (
    FALLBACK_ENDPOINT,
    PRIMARY_ENDPOINT,
    AutocompleteProvider,
)
from google_keyword_ai.providers.trends.provider import GoogleTrendsProvider
from google_keyword_ai.ratelimit import AsyncRateLimiter
from google_keyword_ai.storage.engine import open_database

_AUTOCOMPLETE_ENDPOINTS = {PRIMARY_ENDPOINT, FALLBACK_ENDPOINT}


def _contains_cyrillic(text: str) -> bool:
    return any(unicodedata.name(character, "").startswith("CYRILLIC") for character in text)


@pytest.mark.integration
@pytest.mark.anyio
async def test_autocomplete_answers_a_live_query(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    engine = open_database(settings)
    try:
        async with build_client(settings, accept_language="en") as client:
            provider = AutocompleteProvider(
                settings=settings,
                client=client,
                cache=SqliteCache(engine, settings),
                rate_limiter=AsyncRateLimiter(2.0),
            )
            suggestions = await provider.suggest("coffee", Market.parse("en", "US"))
        assert suggestions
        assert all(item.text for item in suggestions)
        assert all(item.source in _AUTOCOMPLETE_ENDPOINTS for item in suggestions)
        assert any(item.relevance is not None for item in suggestions)
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.anyio
async def test_autocomplete_follows_the_requested_market(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    engine = open_database(settings)
    try:
        async with build_client(settings, accept_language="ru") as client:
            provider = AutocompleteProvider(
                settings=settings,
                client=client,
                cache=SqliteCache(engine, settings),
                rate_limiter=AsyncRateLimiter(2.0),
            )
            suggestions = await provider.suggest("кофе", Market.parse("ru", "RU"))
        assert suggestions
        assert any(_contains_cyrillic(item.text) for item in suggestions)
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.anyio
async def test_trends_returns_a_timeline_for_one_keyword(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    engine = open_database(settings)
    try:
        async with build_client(settings, accept_language="en") as client:
            provider = GoogleTrendsProvider(
                settings=settings,
                client=client,
                cache=SqliteCache(engine, settings),
                rate_limiter=AsyncRateLimiter(1.0 / settings.trends_pacing_seconds),
            )
            result = await provider.fetch(["coffee"], geo="US", timeframe="today 12-m", hl="en")
        assert len(result.timeline) >= 8
        assert all(len(point.values) == 1 for point in result.timeline)
        assert len(result.normalization_scope) == 16
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.anyio
async def test_trends_comparison_splits_related_queries_per_keyword(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    engine = open_database(settings)
    try:
        async with build_client(settings, accept_language="en") as client:
            provider = GoogleTrendsProvider(
                settings=settings,
                client=client,
                cache=SqliteCache(engine, settings),
                rate_limiter=AsyncRateLimiter(1.0 / settings.trends_pacing_seconds),
            )
            result = await provider.fetch(
                ["coffee", "tea"], geo="US", timeframe="today 12-m", hl="en"
            )
        assert len(result.timeline) >= 8
        assert any(
            "RELATED_QUERIES came back once per keyword" in warning for warning in provider.warnings
        )
    finally:
        engine.dispose()
