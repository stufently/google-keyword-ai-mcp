from collections.abc import Sequence
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
from google_keyword_ai.expansion import ExpansionStrategy
from google_keyword_ai.http import build_client
from google_keyword_ai.market import Market
from google_keyword_ai.normalize import KeywordCandidate
from google_keyword_ai.providers.autocomplete import AutocompleteProvider
from google_keyword_ai.providers.base import ProviderInfo
from google_keyword_ai.providers.expander import ExpansionLimits, ExpansionStats, KeywordExpander
from google_keyword_ai.ratelimit import AsyncRateLimiter
from google_keyword_ai.storage.engine import open_database

# Running out of budget means the answer is cut short. Reaching the depth the
# caller asked for does not: that is the requested scope, finished. Treating it
# as partial would make every ordinary run report partial and exit non-zero.
BUDGET_STOPS = frozenset({"max_queries", "max_results", "max_runtime"})


class ExpandData(BaseModel):
    seed: str
    language: str
    country: str
    provider: ProviderInfo
    strategies: list[str]
    limits: ExpansionLimits
    stats: ExpansionStats
    keywords: list[KeywordCandidate]


async def _fetch_expansion(
    settings: Settings,
    cache: SqliteCache,
    seed: str,
    market: Market,
    limits: ExpansionLimits,
    strategies: Sequence[ExpansionStrategy],
) -> tuple[ProviderInfo, list[KeywordCandidate], ExpansionStats]:
    async with build_client(settings, accept_language=market.language) as client:
        provider = AutocompleteProvider(
            settings=settings,
            client=client,
            cache=cache,
            rate_limiter=AsyncRateLimiter(settings.autocomplete_rate_limit_per_second),
        )
        keywords, stats = await KeywordExpander(provider, limits).expand(
            seed,
            market,
            strategies=strategies,
        )
        return provider.info, keywords, stats


def run_expand(
    settings: Settings,
    seed: str,
    *,
    language: str | None = None,
    country: str | None = None,
    depth: int | None = None,
    max_queries: int | None = None,
    max_results: int | None = None,
    max_runtime_seconds: float | None = None,
    strategies: Sequence[str | ExpansionStrategy] | None = None,
    limit: int | None = None,
) -> Envelope[ExpandData]:
    market = Market.parse(
        settings.default_language if language is None else language,
        settings.default_country if country is None else country,
    )
    limits = ExpansionLimits(
        max_depth=1 if depth is None else depth,
        max_queries=500 if max_queries is None else max_queries,
        max_results=2000 if max_results is None else max_results,
        max_runtime_seconds=120.0 if max_runtime_seconds is None else max_runtime_seconds,
    )
    selected_strategies = (
        list(ExpansionStrategy)
        if strategies is None
        else [
            strategy if isinstance(strategy, ExpansionStrategy) else ExpansionStrategy(strategy)
            for strategy in strategies
        ]
    )
    provider_info = ProviderInfo(name="autocomplete", official=False, stability="unofficial")
    empty_stats = ExpansionStats(queries_executed=0, depth_reached=0)
    engine = open_database(settings)
    try:
        provider_info, keywords, stats = anyio.run(
            partial(
                _fetch_expansion,
                settings,
                SqliteCache(engine, settings),
                seed,
                market,
                limits,
                selected_strategies,
            )
        )
    except (RateLimitError, NetworkError, ApiError, ProviderUnavailableError) as exc:
        data = ExpandData(
            seed=seed,
            language=market.language,
            country=market.country,
            provider=provider_info,
            strategies=[strategy.value for strategy in selected_strategies],
            limits=limits,
            stats=empty_stats,
            keywords=[],
        )
        return Envelope(
            data=data,
            errors=[str(exc)],
            completeness=Completeness.EMPTY,
            completeness_reason=str(exc),
        )
    finally:
        engine.dispose()

    data = ExpandData(
        seed=seed,
        language=market.language,
        country=market.country,
        provider=provider_info,
        strategies=[strategy.value for strategy in selected_strategies],
        limits=limits,
        stats=stats,
        keywords=keywords if limit is None else keywords[:limit],
    )
    if not keywords:
        return Envelope(
            data=data,
            completeness=Completeness.EMPTY,
            completeness_reason="no keywords",
        )
    if stats.stopped_by in BUDGET_STOPS:
        return Envelope(
            data=data,
            completeness=Completeness.PARTIAL,
            completeness_reason=f"stopped by {stats.stopped_by}",
        )
    return Envelope(data=data)
