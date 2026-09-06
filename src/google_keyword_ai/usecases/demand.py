"""Sequential provider orchestration for demand on a common anchor scale."""

from collections.abc import Sequence
from functools import partial

import anyio
from pydantic import BaseModel, Field

from google_keyword_ai.cache import SqliteCache
from google_keyword_ai.config import Settings
from google_keyword_ai.demand import DemandBatch, DemandRow, combine, plan_batches
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
from google_keyword_ai.providers.trends.provider import GoogleTrendsProvider
from google_keyword_ai.ratelimit import AsyncRateLimiter
from google_keyword_ai.storage.engine import open_database

RELATIVE_CAVEAT = "Values are relative to the anchor, not absolute search volumes."
RESOLUTION_CAVEAT = (
    "The scale is tied to the anchor; much weaker keywords hit the anchor's resolution limit."
)
STITCHING_CAVEAT = (
    "Batches are stitched through the anchor; stitching error accumulates from batch to batch."
)


class DemandData(BaseModel):
    provider: ProviderInfo
    anchor: str
    language: str
    country: str
    timeframe: str
    rows: list[DemandRow]
    batches_requested: int
    batches_failed: int
    caveats: list[str]
    notices: list[str] = Field(default_factory=list)


async def _fetch_demand(
    settings: Settings,
    cache: SqliteCache,
    planned: list[list[str]],
    market: Market,
    timeframe: str,
) -> tuple[DemandData, list[str], list[str]]:
    batches: list[DemandBatch] = []
    warnings: list[str] = []
    notices: list[str] = []
    errors: list[str] = []
    async with build_client(settings, accept_language=market.language) as client:
        provider = GoogleTrendsProvider(
            settings=settings,
            client=client,
            cache=cache,
            rate_limiter=AsyncRateLimiter(1.0 / settings.trends_pacing_seconds),
        )
        for keywords in planned:
            try:
                result = await provider.fetch(
                    keywords, geo=market.trends_geo(), timeframe=timeframe, hl=market.language
                )
            except (RateLimitError, NetworkError, ApiError, ProviderUnavailableError) as exc:
                reason = str(exc)
                batches.append(DemandBatch(keywords=keywords, reason=reason))
                errors.append(reason)
                continue
            relevant: list[str] = []
            for warning in provider.warnings:
                if warning.startswith(("RELATED_QUERIES", "GEO_MAP")):
                    notices.append(warning)
                else:
                    relevant.append(warning)
            warnings.extend(relevant)
            # A failed timeline widget can arrive as a parsed, empty result.
            # Preserve the outage reason rather than calling it low demand.
            if not result.timeline and relevant:
                batches.append(DemandBatch(keywords=keywords, reason=relevant[0]))
            else:
                batches.append(DemandBatch(keywords=keywords, result=result))
    data = DemandData(
        provider=provider.info,
        anchor=planned[0][0],
        language=market.language,
        country=market.country,
        timeframe=timeframe,
        rows=combine(batches),
        batches_requested=len(planned),
        batches_failed=sum(batch.result is None for batch in batches),
        caveats=[RELATIVE_CAVEAT, RESOLUTION_CAVEAT, STITCHING_CAVEAT],
        notices=notices,
    )
    return data, warnings, errors


def run_demand(
    settings: Settings,
    keywords: Sequence[str],
    *,
    anchor: str | None = None,
    language: str | None = None,
    country: str | None = None,
    timeframe: str = "today 12-m",
) -> Envelope[DemandData | None]:
    """Rank unique keywords; invalid input is an empty, nullable-data envelope."""
    try:
        planned = plan_batches(
            keywords, anchor if anchor is not None else keywords[0] if keywords else ""
        )
        market = Market.parse(
            settings.default_language if language is None else language,
            settings.default_country if country is None else country,
        )
    except InvalidConfigurationError as exc:
        return Envelope(
            data=None,
            errors=[str(exc)],
            completeness=Completeness.EMPTY,
            completeness_reason=str(exc),
        )
    engine = open_database(settings)
    try:
        data, warnings, errors = anyio.run(
            partial(
                _fetch_demand, settings, SqliteCache(engine, settings), planned, market, timeframe
            )
        )
    finally:
        engine.dispose()
    numeric = sum(row.relative_demand is not None for row in data.rows)
    if data.batches_failed == data.batches_requested or numeric == 0:
        completeness = Completeness.EMPTY
    elif data.batches_failed or numeric < len(data.rows):
        completeness = Completeness.PARTIAL
    else:
        completeness = Completeness.COMPLETE
    reason = None
    if completeness is not Completeness.COMPLETE:
        reason = next(
            iter(errors or warnings),
            next((row.reason for row in data.rows if row.reason), "No comparable demand data."),
        )
    return Envelope(
        data=data,
        warnings=warnings,
        errors=errors,
        completeness=completeness,
        completeness_reason=reason,
    )
