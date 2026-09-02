import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial

import anyio

from google_keyword_ai.cache import SqliteCache
from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.errors import GkaiError, InvalidConfigurationError
from google_keyword_ai.http import build_client
from google_keyword_ai.market import Market
from google_keyword_ai.pipeline.budget import Budget, BudgetGuard, BudgetSpend
from google_keyword_ai.pipeline.models import DataQuality, DryRunPlan, ResearchData, ResearchStats
from google_keyword_ai.pipeline.scenarios import (
    GENERAL_CAVEAT,
    CompetitorResearch,
    ExistingSiteResearch,
    NewNicheResearch,
    ScenarioContext,
)
from google_keyword_ai.providers.autocomplete import AutocompleteProvider
from google_keyword_ai.providers.expander import ExpansionLimits, KeywordExpander
from google_keyword_ai.providers.trends.provider import GoogleTrendsProvider
from google_keyword_ai.ratelimit import AsyncRateLimiter
from google_keyword_ai.storage.engine import open_database
from google_keyword_ai.usecases.ads import _build_provider as build_ads_provider
from google_keyword_ai.usecases.gsc import _build_provider as build_gsc_provider

_SCENARIOS = frozenset({"auto", "niche", "competitor", "site"})
_DOMAIN = re.compile(
    r"^(?:https?://)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
    r"(?::\d+)?(?:[/?#].*)?$",
    re.IGNORECASE,
)


def _looks_like_domain_or_url(target: str) -> bool:
    return bool(_DOMAIN.fullmatch(target.strip()))


def _scenario_for_name(
    name: str,
    target: str,
    seed_keyword: str | None,
) -> NewNicheResearch | CompetitorResearch | ExistingSiteResearch:
    if name == "niche":
        return NewNicheResearch(target)
    if name == "competitor":
        return CompetitorResearch(target, seed_keyword)
    if name == "site":
        return ExistingSiteResearch(target)
    raise InvalidConfigurationError(f"Unknown research scenario: {name}.")


def _dry_scenario(
    scenario: str,
    target: str,
    seed_keyword: str | None,
) -> NewNicheResearch | CompetitorResearch | ExistingSiteResearch:
    if scenario != "auto":
        return _scenario_for_name(scenario, target, seed_keyword)
    if target.startswith("sc-domain:"):
        return ExistingSiteResearch(target)
    if _looks_like_domain_or_url(target):
        return CompetitorResearch(target, seed_keyword)
    return NewNicheResearch(target)


async def _matches_property(context: ScenarioContext, target: str) -> bool:
    if context.search_console is None or not (
        target.startswith("sc-domain:") or target.startswith("https://")
    ):
        return False
    try:
        properties = await context.search_console.list_properties()
    except GkaiError as exc:
        context.warnings.append(f"Search Console properties could not be listed: {exc}")
        return False
    normalized = target.rstrip("/")
    return any(prop.site_url.rstrip("/") == normalized for prop in properties)


async def _select_scenario(
    context: ScenarioContext,
    scenario: str,
    target: str,
    seed_keyword: str | None,
) -> NewNicheResearch | CompetitorResearch | ExistingSiteResearch:
    if scenario != "auto":
        return _scenario_for_name(scenario, target, seed_keyword)
    if await _matches_property(context, target):
        return ExistingSiteResearch(target)
    if _looks_like_domain_or_url(target) or target.startswith("sc-domain:"):
        return CompetitorResearch(target, seed_keyword)
    return NewNicheResearch(target)


def _dry_context(settings: Settings, market: Market, budget: Budget) -> ScenarioContext:
    return ScenarioContext(
        settings=settings,
        market=market,
        budget_guard=BudgetGuard(budget),
        availability={
            "autocomplete": True,
            "google_ads": bool(settings.google_ads_customer_id),
            "trends": settings.trends_enabled,
            "search_console": settings.search_console_credentials_path is not None,
        },
    )


@asynccontextmanager
async def _live_context(
    settings: Settings,
    market: Market,
    budget: Budget,
    cache: SqliteCache,
) -> AsyncIterator[ScenarioContext]:
    async with build_client(settings, accept_language=market.language) as client:
        autocomplete = AutocompleteProvider(
            settings=settings,
            client=client,
            cache=cache,
            rate_limiter=AsyncRateLimiter(settings.autocomplete_rate_limit_per_second),
        )
        ads_candidate = build_ads_provider(settings, cache)
        ads = ads_candidate if ads_candidate.is_available() else None
        gsc_candidate = build_gsc_provider(settings, cache)
        gsc = gsc_candidate if gsc_candidate.is_available() else None
        trends_candidate = GoogleTrendsProvider(
            settings=settings,
            client=client,
            cache=cache,
            rate_limiter=AsyncRateLimiter(1.0 / settings.trends_pacing_seconds),
        )
        trends = trends_candidate if trends_candidate.is_available() else None
        expander = KeywordExpander(
            autocomplete,
            ExpansionLimits(
                max_depth=1,
                max_queries=budget.max_autocomplete_queries,
                max_results=budget.max_keywords,
                max_runtime_seconds=budget.max_runtime_seconds,
            ),
        )
        yield ScenarioContext(
            settings=settings,
            market=market,
            budget_guard=BudgetGuard(budget),
            autocomplete=autocomplete,
            google_ads=ads,
            trends=trends,
            search_console=gsc,
            expander=expander,
            availability={
                "autocomplete": True,
                "google_ads": ads is not None,
                "trends": trends is not None,
                "search_console": gsc is not None,
            },
        )


async def _execute(
    settings: Settings,
    target: str,
    scenario: str,
    market: Market,
    seed_keyword: str | None,
    budget: Budget,
    limit: int | None,
) -> tuple[ResearchData, list[str], list[str]]:
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)
        async with _live_context(settings, market, budget, cache) as context:
            selected = await _select_scenario(context, scenario, target, seed_keyword)
            data = await selected.run(context)
            if limit is not None:
                data.keywords = data.keywords[:limit]
            return data, context.warnings, context.errors
    finally:
        engine.dispose()


def run_research(
    settings: Settings,
    target: str,
    *,
    scenario: str = "auto",
    language: str | None = None,
    country: str | None = None,
    seed_keyword: str | None = None,
    budget: Budget | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> Envelope[ResearchData] | Envelope[DryRunPlan]:
    if scenario not in _SCENARIOS:
        raise InvalidConfigurationError(f"Unknown research scenario: {scenario}.")
    if limit is not None and limit <= 0:
        raise InvalidConfigurationError("Research limit must be positive.")
    market = Market.parse(
        settings.default_language if language is None else language,
        settings.default_country if country is None else country,
    )
    active_budget = Budget() if budget is None else budget
    if dry_run:
        context = _dry_context(settings, market, active_budget)
        selected = _dry_scenario(scenario, target, seed_keyword)
        return Envelope(data=selected.plan(context))

    try:
        data, warnings, errors = anyio.run(
            partial(
                _execute,
                settings,
                target,
                scenario,
                market,
                seed_keyword,
                active_budget,
                limit,
            )
        )
    except GkaiError as exc:
        empty_context = _dry_context(settings, market, active_budget)
        selected = _dry_scenario(scenario, target, seed_keyword)
        empty = ResearchData(
            scenario=selected.plan(empty_context).scenario,
            input=target,
            language=market.language,
            country=market.country,
            keywords=[],
            stats=ResearchStats(spend=BudgetSpend()),
            data_quality=DataQuality(
                sources=selected.plan(empty_context).sources,
                retrieved_at=datetime.now(UTC),
                absolute_metrics=[],
                relative_metrics=[],
                derived_metrics=[],
                caveats=[GENERAL_CAVEAT],
            ),
        )
        return Envelope(
            data=empty,
            errors=[str(exc)],
            completeness=Completeness.EMPTY,
            completeness_reason=str(exc),
        )

    has_data = bool(data.keywords or data.trends is not None or data.opportunities)
    if not has_data:
        reason = errors[0] if errors else warnings[-1] if warnings else "no research data"
        return Envelope(
            data=data,
            warnings=warnings,
            errors=errors,
            completeness=Completeness.EMPTY,
            completeness_reason=reason,
        )
    if warnings or errors or data.stats.stopped_by is not None:
        reason = (
            errors[0]
            if errors
            else warnings[0]
            if warnings
            else f"stopped by {data.stats.stopped_by}"
        )
        return Envelope(
            data=data,
            warnings=warnings,
            errors=errors,
            completeness=Completeness.PARTIAL,
            completeness_reason=reason,
        )
    return Envelope(data=data)
