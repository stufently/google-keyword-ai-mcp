import hashlib
from collections.abc import Sequence
from functools import partial

import anyio
from pydantic import BaseModel

from google_keyword_ai.cache import SqliteCache
from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.errors import (
    ApiError,
    AuthenticationError,
    NetworkError,
    ProviderUnavailableError,
    RateLimitError,
)
from google_keyword_ai.market import Market
from google_keyword_ai.providers.base import ProviderInfo
from google_keyword_ai.providers.google_ads import AdsSeed, GoogleAdsProvider, KeywordIdea
from google_keyword_ai.ratelimit import InterProcessRateLimiter
from google_keyword_ai.storage.engine import open_database
from google_keyword_ai.targets import is_bare_domain
from google_keyword_ai.usecases.limits import require_positive_limit


class AdsData(BaseModel):
    provider: ProviderInfo
    mode: str
    language: str
    country: str
    ideas: list[KeywordIdea]


def _build_provider(settings: Settings, cache: SqliteCache) -> GoogleAdsProvider:
    customer_scope = settings.google_ads_customer_id or "missing"
    scope_hash = hashlib.sha256(customer_scope.encode()).hexdigest()[:16]
    rate_limiter = InterProcessRateLimiter(
        settings.google_ads_rate_limit_per_second,
        settings.data_dir / f"google-ads-{scope_hash}.lock",
    )
    return GoogleAdsProvider(settings=settings, cache=cache, rate_limiter=rate_limiter)


async def _fetch_ideas(
    settings: Settings,
    cache: SqliteCache,
    seed: AdsSeed,
    market: Market,
    include_adult: bool,
) -> tuple[ProviderInfo, list[KeywordIdea]]:
    provider = _build_provider(settings, cache)
    ideas = await provider.keyword_ideas(seed, market, include_adult=include_adult)
    return provider.info, ideas


async def _fetch_historical(
    settings: Settings,
    cache: SqliteCache,
    keywords: Sequence[str],
    market: Market,
) -> tuple[ProviderInfo, list[KeywordIdea]]:
    provider = _build_provider(settings, cache)
    ideas = await provider.historical_metrics(keywords, market)
    return provider.info, ideas


def _empty_envelope(
    provider: ProviderInfo,
    mode: str,
    market: Market,
    reason: str,
    *,
    error: bool,
) -> Envelope[AdsData]:
    return Envelope(
        data=AdsData(
            provider=provider,
            mode=mode,
            language=market.language,
            country=market.country,
            ideas=[],
        ),
        errors=[reason] if error else [],
        completeness=Completeness.EMPTY,
        completeness_reason=reason,
    )


def run_ads_ideas(
    settings: Settings,
    keywords: Sequence[str] | None = None,
    *,
    url: str | None = None,
    site: str | None = None,
    language: str | None = None,
    country: str | None = None,
    include_adult: bool = False,
    limit: int | None = None,
) -> Envelope[AdsData]:
    require_positive_limit(limit, "Keyword idea")
    market = Market.parse(
        settings.default_language if language is None else language,
        settings.default_country if country is None else country,
    )
    seed = AdsSeed(keywords=[] if keywords is None else list(keywords), url=url, site=site)
    mode = seed.mode()
    provider_info = ProviderInfo(name="google_ads", official=True, stability="stable")
    engine = open_database(settings)
    try:
        provider_info, ideas = anyio.run(
            partial(
                _fetch_ideas,
                settings,
                SqliteCache(engine, settings),
                seed,
                market,
                include_adult,
            )
        )
    except (
        ProviderUnavailableError,
        AuthenticationError,
        RateLimitError,
        NetworkError,
        ApiError,
    ) as exc:
        return _empty_envelope(provider_info, mode, market, str(exc), error=True)
    finally:
        engine.dispose()

    if limit is not None:
        ideas = ideas[:limit]
    if not ideas:
        return _empty_envelope(
            provider_info,
            mode,
            market,
            "no keyword ideas",
            error=False,
        )
    return Envelope(
        data=AdsData(
            provider=provider_info,
            mode=mode,
            language=market.language,
            country=market.country,
            ideas=ideas,
        )
    )


def run_ads_historical(
    settings: Settings,
    keywords: Sequence[str],
    *,
    language: str | None = None,
    country: str | None = None,
) -> Envelope[AdsData]:
    market = Market.parse(
        settings.default_language if language is None else language,
        settings.default_country if country is None else country,
    )
    mode = "historical_metrics"
    provider_info = ProviderInfo(name="google_ads", official=True, stability="stable")
    engine = open_database(settings)
    try:
        provider_info, ideas = anyio.run(
            partial(
                _fetch_historical,
                settings,
                SqliteCache(engine, settings),
                list(keywords),
                market,
            )
        )
    except (
        ProviderUnavailableError,
        AuthenticationError,
        RateLimitError,
        NetworkError,
        ApiError,
    ) as exc:
        return _empty_envelope(provider_info, mode, market, str(exc), error=True)
    finally:
        engine.dispose()

    if not ideas:
        return _empty_envelope(
            provider_info,
            mode,
            market,
            "no keyword ideas",
            error=False,
        )
    return Envelope(
        data=AdsData(
            provider=provider_info,
            mode=mode,
            language=market.language,
            country=market.country,
            ideas=ideas,
        )
    )


def run_competitor(
    settings: Settings,
    target: str,
    *,
    seed_keyword: str | None = None,
    language: str | None = None,
    country: str | None = None,
    limit: int | None = None,
) -> Envelope[AdsData]:
    if seed_keyword is not None:
        return run_ads_ideas(
            settings,
            [seed_keyword],
            url=target,
            language=language,
            country=country,
            limit=limit,
        )
    if is_bare_domain(target):
        return run_ads_ideas(
            settings,
            site=target,
            language=language,
            country=country,
            limit=limit,
        )
    return run_ads_ideas(
        settings,
        url=target,
        language=language,
        country=country,
        limit=limit,
    )
