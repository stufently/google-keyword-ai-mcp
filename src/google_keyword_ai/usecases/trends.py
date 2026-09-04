from collections.abc import Sequence
from datetime import UTC, datetime
from functools import partial

import anyio
from pydantic import BaseModel

from google_keyword_ai.cache import SqliteCache
from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.errors import (
    ApiError,
    InvalidConfigurationError,
    NetworkError,
    ProviderUnavailableError,
    RateLimitError,
)
from google_keyword_ai.http import build_client
from google_keyword_ai.market import Market
from google_keyword_ai.providers.base import ProviderInfo
from google_keyword_ai.providers.trends.models import (
    TrendsResult,
    build_normalization_scope,
)
from google_keyword_ai.providers.trends.provider import GoogleTrendsProvider
from google_keyword_ai.providers.trends.unofficial import EXPLORE_URL
from google_keyword_ai.ratelimit import AsyncRateLimiter
from google_keyword_ai.storage.engine import open_database


class TrendsData(BaseModel):
    provider: ProviderInfo
    result: TrendsResult


async def _fetch_trends(
    settings: Settings,
    cache: SqliteCache,
    keywords: list[str],
    market: Market,
    timeframe: str,
) -> tuple[ProviderInfo, TrendsResult, list[str]]:
    async with build_client(settings, accept_language=market.language) as client:
        provider = GoogleTrendsProvider(
            settings=settings,
            client=client,
            cache=cache,
            rate_limiter=AsyncRateLimiter(1.0 / settings.trends_pacing_seconds),
        )
        result = await provider.fetch(
            keywords,
            geo=market.trends_geo(),
            timeframe=timeframe,
            hl=market.language,
        )
        return provider.info, result, list(provider.warnings)


def _empty_result(keywords: list[str], market: Market, timeframe: str) -> TrendsResult:
    return TrendsResult(
        keywords=keywords,
        geo=market.trends_geo(),
        timeframe=timeframe,
        normalization_scope=build_normalization_scope(
            keywords,
            geo=market.trends_geo(),
            timeframe=timeframe,
            hl=market.language,
        ),
        retrieved_at=datetime.now(UTC),
        source=EXPLORE_URL,
    )


def _run_trends(
    settings: Settings,
    keywords: list[str],
    *,
    language: str | None,
    country: str | None,
    timeframe: str,
) -> Envelope[TrendsData]:
    market = Market.parse(
        settings.default_language if language is None else language,
        settings.default_country if country is None else country,
    )
    provider_info = ProviderInfo(name="trends", official=False, stability="unofficial")
    engine = open_database(settings)
    try:
        provider_info, result, warnings = anyio.run(
            partial(
                _fetch_trends,
                settings,
                SqliteCache(engine, settings),
                keywords,
                market,
                timeframe,
            )
        )
    except (RateLimitError, NetworkError, ApiError, ProviderUnavailableError) as exc:
        data = TrendsData(
            provider=provider_info,
            result=_empty_result(keywords, market, timeframe),
        )
        return Envelope(
            data=data,
            errors=[str(exc)],
            completeness=Completeness.EMPTY,
            completeness_reason=str(exc),
        )
    finally:
        engine.dispose()

    data = TrendsData(provider=provider_info, result=result)
    if not result.carries_data():
        # Nothing came back at all. A failed widget is why, when there was one:
        # calling that `partial` would promise data the payload does not hold,
        # and the flat "no trend data" would read as Google's verdict on the
        # keyword rather than as a request that never landed.
        return Envelope(
            data=data,
            warnings=warnings,
            completeness=Completeness.EMPTY,
            completeness_reason=warnings[0] if warnings else "no trend data",
        )
    if warnings:
        # The first warning, not a fixed sentence about failure. Not every
        # warning is a failure: a comparison splits related queries one per
        # keyword, and reporting that as "one or more trend widgets failed"
        # tells the caller something went wrong on a request where nothing did.
        return Envelope(
            data=data,
            warnings=warnings,
            completeness=Completeness.PARTIAL,
            completeness_reason=warnings[0],
        )
    return Envelope(data=data)


def run_trends(
    settings: Settings,
    keyword: str,
    *,
    language: str | None = None,
    country: str | None = None,
    timeframe: str = "today 12-m",
) -> Envelope[TrendsData]:
    return _run_trends(
        settings,
        [keyword],
        language=language,
        country=country,
        timeframe=timeframe,
    )


def run_trends_compare(
    settings: Settings,
    keywords: Sequence[str],
    *,
    language: str | None = None,
    country: str | None = None,
    timeframe: str = "today 12-m",
) -> Envelope[TrendsData]:
    keyword_list = list(keywords)
    if not 1 <= len(keyword_list) <= 5:
        raise InvalidConfigurationError(
            "Google Trends comparison requires between one and five keywords."
        )
    return _run_trends(
        settings,
        keyword_list,
        language=language,
        country=country,
        timeframe=timeframe,
    )
