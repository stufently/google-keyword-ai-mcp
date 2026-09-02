from functools import partial

import anyio
from pydantic import BaseModel

from google_keyword_ai.cache import SqliteCache
from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.errors import (
    ApiError,
    NetworkError,
    ProviderUnavailableError,
    RateLimitError,
)
from google_keyword_ai.http import build_client
from google_keyword_ai.market import Market
from google_keyword_ai.providers.autocomplete import AutocompleteProvider, Suggestion
from google_keyword_ai.providers.base import ProviderInfo
from google_keyword_ai.ratelimit import AsyncRateLimiter
from google_keyword_ai.storage.engine import open_database


class SuggestData(BaseModel):
    query: str
    language: str
    country: str
    provider: ProviderInfo
    suggestions: list[Suggestion]


async def _fetch_suggestions(
    settings: Settings,
    cache: SqliteCache,
    query: str,
    market: Market,
    limit: int | None,
) -> tuple[ProviderInfo, list[Suggestion]]:
    async with build_client(settings, accept_language=market.language) as client:
        provider = AutocompleteProvider(
            settings=settings,
            client=client,
            cache=cache,
            rate_limiter=AsyncRateLimiter(settings.autocomplete_rate_limit_per_second),
        )
        suggestions = await provider.suggest(query, market, limit=limit)
        return provider.info, suggestions


def run_suggest(
    settings: Settings,
    query: str,
    *,
    language: str | None = None,
    country: str | None = None,
    limit: int | None = None,
) -> Envelope[SuggestData]:
    market = Market.parse(
        settings.default_language if language is None else language,
        settings.default_country if country is None else country,
    )
    engine = open_database(settings)
    provider_info = ProviderInfo(name="autocomplete", official=False, stability="unofficial")
    try:
        provider_info, suggestions = anyio.run(
            partial(
                _fetch_suggestions,
                settings,
                SqliteCache(engine, settings),
                query,
                market,
                limit,
            )
        )
    except (RateLimitError, NetworkError, ApiError, ProviderUnavailableError) as exc:
        data = SuggestData(
            query=query,
            language=market.language,
            country=market.country,
            provider=provider_info,
            suggestions=[],
        )
        return Envelope(
            data=data,
            errors=[str(exc)],
            completeness=Completeness.EMPTY,
            completeness_reason=str(exc),
        )
    finally:
        engine.dispose()

    data = SuggestData(
        query=query,
        language=market.language,
        country=market.country,
        provider=provider_info,
        suggestions=suggestions,
    )
    if not suggestions:
        return Envelope(
            data=data,
            completeness=Completeness.EMPTY,
            completeness_reason="no suggestions",
        )
    return Envelope(data=data)
