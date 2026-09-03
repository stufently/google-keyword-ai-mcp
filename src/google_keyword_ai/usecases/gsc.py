from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from functools import partial

import anyio
from pydantic import BaseModel

from google_keyword_ai.cache import SqliteCache
from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.errors import (
    ApiError,
    AuthenticationError,
    InvalidConfigurationError,
    NetworkError,
    ProviderUnavailableError,
    RateLimitError,
)
from google_keyword_ai.market import Market
from google_keyword_ai.opportunities import Opportunity, find_opportunities
from google_keyword_ai.providers.base import ProviderInfo
from google_keyword_ai.providers.search_console import (
    SearchAnalyticsPage,
    SearchAnalyticsRow,
    SearchConsoleProvider,
    SiteProperty,
)
from google_keyword_ai.ratelimit import InterProcessRateLimiter
from google_keyword_ai.storage.engine import open_database
from google_keyword_ai.usecases.limits import require_positive_limit

_GSC_ERRORS = (
    ProviderUnavailableError,
    AuthenticationError,
    RateLimitError,
    NetworkError,
    ApiError,
    InvalidConfigurationError,
)


class PropertiesData(BaseModel):
    provider: ProviderInfo
    properties: list[SiteProperty]


class QueriesData(BaseModel):
    provider: ProviderInfo
    site_url: str
    start_date: str
    end_date: str
    dimensions: list[str]
    rows: list[SearchAnalyticsRow]
    truncated: bool
    truncation_reason: str | None


class OpportunitiesData(BaseModel):
    provider: ProviderInfo
    site_url: str
    start_date: str
    end_date: str
    thresholds: dict[str, float]
    opportunities: list[Opportunity]
    truncated: bool


def _provider_info() -> ProviderInfo:
    return ProviderInfo(name="search_console", official=True, stability="stable")


def _build_provider(settings: Settings, cache: SqliteCache) -> SearchConsoleProvider:
    return SearchConsoleProvider(
        settings=settings,
        cache=cache,
        rate_limiter=InterProcessRateLimiter(
            settings.search_console_rate_limit_per_second,
            settings.data_dir / "search-console.lock",
        ),
    )


def _parse_date(value: str | date, field_name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidConfigurationError(f"{field_name} must use YYYY-MM-DD format.") from exc


def _date_window(
    *,
    days: int,
    start_date: str | date | None,
    end_date: str | date | None,
) -> tuple[date, date]:
    if days <= 0:
        raise InvalidConfigurationError("days must be positive.")
    resolved_end = (
        datetime.now(UTC).date() - timedelta(days=2)
        if end_date is None
        else _parse_date(end_date, "end_date")
    )
    resolved_start = (
        resolved_end - timedelta(days=days - 1)
        if start_date is None
        else _parse_date(start_date, "start_date")
    )
    if resolved_start > resolved_end:
        raise InvalidConfigurationError("start_date must not be after end_date.")
    return resolved_start, resolved_end


def _market(settings: Settings, country: str | None) -> Market | None:
    if country is None:
        return None
    return Market.parse(settings.default_language, country)


async def _fetch_properties(
    settings: Settings, cache: SqliteCache
) -> tuple[ProviderInfo, list[SiteProperty]]:
    provider = _build_provider(settings, cache)
    return provider.info, await provider.list_properties()


async def _fetch_queries(
    settings: Settings,
    cache: SqliteCache,
    site_url: str,
    start_date: date,
    end_date: date,
    dimensions: Sequence[str],
    market: Market | None,
    search_type: str,
) -> tuple[ProviderInfo, SearchAnalyticsPage]:
    provider = _build_provider(settings, cache)
    page = await provider.query(
        site_url,
        start_date=start_date,
        end_date=end_date,
        dimensions=dimensions,
        market=market,
        search_type=search_type,
    )
    return provider.info, page


def _error_queries(
    provider: ProviderInfo,
    site_url: str,
    start_date: str,
    end_date: str,
    dimensions: list[str],
    reason: str,
) -> Envelope[QueriesData]:
    return Envelope(
        data=QueriesData(
            provider=provider,
            site_url=site_url,
            start_date=start_date,
            end_date=end_date,
            dimensions=dimensions,
            rows=[],
            truncated=False,
            truncation_reason=None,
        ),
        errors=[reason],
        completeness=Completeness.EMPTY,
        completeness_reason=reason,
    )


def run_gsc_properties(settings: Settings) -> Envelope[PropertiesData]:
    provider = _provider_info()
    try:
        engine = open_database(settings)
    except _GSC_ERRORS as exc:
        reason = str(exc)
        return Envelope(
            data=PropertiesData(provider=provider, properties=[]),
            errors=[reason],
            completeness=Completeness.EMPTY,
            completeness_reason=reason,
        )
    try:
        provider, properties = anyio.run(
            partial(_fetch_properties, settings, SqliteCache(engine, settings))
        )
    except _GSC_ERRORS as exc:
        reason = str(exc)
        return Envelope(
            data=PropertiesData(provider=provider, properties=[]),
            errors=[reason],
            completeness=Completeness.EMPTY,
            completeness_reason=reason,
        )
    finally:
        engine.dispose()
    if not properties:
        return Envelope(
            data=PropertiesData(provider=provider, properties=[]),
            completeness=Completeness.EMPTY,
            completeness_reason="no Search Console properties",
        )
    return Envelope(data=PropertiesData(provider=provider, properties=properties))


def run_gsc_queries(
    settings: Settings,
    site_url: str,
    *,
    days: int = 28,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    dimensions: Sequence[str] | None = None,
    country: str | None = None,
    search_type: str = "web",
    limit: int | None = None,
) -> Envelope[QueriesData]:
    require_positive_limit(limit, "Query")
    provider = _provider_info()
    requested_dimensions = ["query"] if dimensions is None else list(dimensions)
    start_text = "" if start_date is None else str(start_date)
    end_text = "" if end_date is None else str(end_date)
    try:
        start, end = _date_window(days=days, start_date=start_date, end_date=end_date)
        start_text = start.isoformat()
        end_text = end.isoformat()
        market = _market(settings, country)
    except _GSC_ERRORS as exc:
        return _error_queries(
            provider,
            site_url,
            start_text,
            end_text,
            requested_dimensions,
            str(exc),
        )

    try:
        engine = open_database(settings)
    except _GSC_ERRORS as exc:
        return _error_queries(
            provider,
            site_url,
            start_text,
            end_text,
            requested_dimensions,
            str(exc),
        )
    try:
        provider, page = anyio.run(
            partial(
                _fetch_queries,
                settings,
                SqliteCache(engine, settings),
                site_url,
                start,
                end,
                requested_dimensions,
                market,
                search_type,
            )
        )
    except _GSC_ERRORS as exc:
        return _error_queries(
            provider,
            site_url,
            start_text,
            end_text,
            requested_dimensions,
            str(exc),
        )
    finally:
        engine.dispose()

    rows = page.rows if limit is None else page.rows[:limit]
    data = QueriesData(
        provider=provider,
        site_url=site_url,
        start_date=start_text,
        end_date=end_text,
        dimensions=requested_dimensions,
        rows=rows,
        truncated=page.truncated,
        truncation_reason=page.truncation_reason,
    )
    if page.truncated:
        reason = page.truncation_reason or "Search Console result was truncated."
        return Envelope(
            data=data,
            warnings=[reason],
            completeness=Completeness.PARTIAL,
            completeness_reason=reason,
        )
    if not rows:
        return Envelope(
            data=data,
            completeness=Completeness.EMPTY,
            completeness_reason="no search analytics data",
        )
    return Envelope(data=data)


def _thresholds(settings: Settings) -> dict[str, float]:
    return {
        "min_impressions": float(settings.gsc_opportunity_min_impressions),
        "min_position": settings.gsc_opportunity_min_position,
        "max_position": settings.gsc_opportunity_max_position,
        "max_ctr": settings.gsc_opportunity_max_ctr,
    }


def _error_opportunities(
    provider: ProviderInfo,
    settings: Settings,
    site_url: str,
    start_date: str,
    end_date: str,
    reason: str,
) -> Envelope[OpportunitiesData]:
    return Envelope(
        data=OpportunitiesData(
            provider=provider,
            site_url=site_url,
            start_date=start_date,
            end_date=end_date,
            thresholds=_thresholds(settings),
            opportunities=[],
            truncated=False,
        ),
        errors=[reason],
        completeness=Completeness.EMPTY,
        completeness_reason=reason,
    )


def run_gsc_opportunities(
    settings: Settings,
    site_url: str,
    *,
    days: int = 28,
    country: str | None = None,
    limit: int | None = None,
) -> Envelope[OpportunitiesData]:
    require_positive_limit(limit, "Opportunity")
    provider = _provider_info()
    try:
        start, end = _date_window(days=days, start_date=None, end_date=None)
        market = _market(settings, country)
    except _GSC_ERRORS as exc:
        return _error_opportunities(provider, settings, site_url, "", "", str(exc))

    try:
        engine = open_database(settings)
    except _GSC_ERRORS as exc:
        return _error_opportunities(
            provider,
            settings,
            site_url,
            start.isoformat(),
            end.isoformat(),
            str(exc),
        )
    try:
        provider, page = anyio.run(
            partial(
                _fetch_queries,
                settings,
                SqliteCache(engine, settings),
                site_url,
                start,
                end,
                ["query", "page"],
                market,
                "web",
            )
        )
    except _GSC_ERRORS as exc:
        return _error_opportunities(
            provider,
            settings,
            site_url,
            start.isoformat(),
            end.isoformat(),
            str(exc),
        )
    finally:
        engine.dispose()

    opportunities = find_opportunities(page.rows, settings)
    if limit is not None:
        opportunities = opportunities[:limit]
    data = OpportunitiesData(
        provider=provider,
        site_url=site_url,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        thresholds=_thresholds(settings),
        opportunities=opportunities,
        truncated=page.truncated,
    )
    if page.truncated:
        reason = page.truncation_reason or "Search Console result was truncated."
        return Envelope(
            data=data,
            warnings=[reason],
            completeness=Completeness.PARTIAL,
            completeness_reason=reason,
        )
    if not opportunities:
        return Envelope(
            data=data,
            completeness=Completeness.EMPTY,
            completeness_reason="no search analytics data",
        )
    return Envelope(data=data)
