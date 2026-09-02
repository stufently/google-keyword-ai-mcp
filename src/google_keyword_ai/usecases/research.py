import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial

import anyio

from google_keyword_ai import __version__
from google_keyword_ai.cache import PARSER_VERSION, SqliteCache
from google_keyword_ai.config import Settings, masked_dump
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.errors import GkaiError, InvalidConfigurationError
from google_keyword_ai.http import build_client
from google_keyword_ai.market import Market
from google_keyword_ai.pipeline.budget import Budget, BudgetGuard, BudgetSpend
from google_keyword_ai.pipeline.executor import RunExecutor, scenario_stages
from google_keyword_ai.pipeline.models import DataQuality, DryRunPlan, ResearchData, ResearchStats
from google_keyword_ai.pipeline.runs import (
    RunRecord,
    RunStatus,
    RunStore,
    StageRecord,
    StageStatus,
    new_run_id,
    stage_fingerprint,
)
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


def _envelope_for_research(
    data: ResearchData,
    warnings: list[str],
    errors: list[str],
    *,
    run_id: str | None = None,
) -> Envelope[ResearchData]:
    has_data = bool(data.keywords or data.trends is not None or data.opportunities)
    if not has_data:
        reason = errors[0] if errors else warnings[-1] if warnings else "no research data"
        return Envelope(
            data=data,
            warnings=warnings,
            errors=errors,
            completeness=Completeness.EMPTY,
            completeness_reason=reason,
            run_id=run_id,
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
            run_id=run_id,
        )
    return Envelope(data=data, run_id=run_id)


async def _execute_saved(
    settings: Settings,
    target: str,
    scenario: str,
    market: Market,
    seed_keyword: str | None,
    budget: Budget,
    limit: int | None,
) -> Envelope[ResearchData]:
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)
        store = RunStore(engine)
        preliminary = _dry_scenario(scenario, target, seed_keyword)
        preliminary_name = preliminary.plan(_dry_context(settings, market, budget)).scenario
        stages = scenario_stages(
            preliminary_name,
            target=target,
            market=market,
            budget=budget,
            seed_keyword=seed_keyword,
        )
        pending_stages = [
            StageRecord(
                name=stage.name,
                position=stage.position,
                status=StageStatus.PENDING,
                fingerprint=stage_fingerprint(stage.name, stage.fingerprint_payload),
            )
            for stage in stages
        ]
        now = datetime.now(UTC)
        record = RunRecord(
            run_id=new_run_id(),
            scenario=preliminary_name,
            target=target,
            language=market.language,
            country=market.country,
            status=RunStatus.RUNNING,
            app_version=__version__,
            parser_version=PARSER_VERSION,
            budget=budget,
            config_snapshot=masked_dump(settings),
            created_at=now,
            updated_at=now,
            stages=pending_stages,
        )
        store.create(record)
        async with _live_context(settings, market, budget, cache) as context:
            selected = await _select_scenario(context, scenario, target, seed_keyword)
            scenario_name = selected.plan(_dry_context(settings, market, budget)).scenario
            if scenario_name != preliminary_name:
                stages = scenario_stages(
                    scenario_name,
                    target=target,
                    market=market,
                    budget=budget,
                    seed_keyword=seed_keyword,
                )
                pending_stages = [
                    StageRecord(
                        name=stage.name,
                        position=stage.position,
                        status=StageStatus.PENDING,
                        fingerprint=stage_fingerprint(
                            stage.name,
                            stage.fingerprint_payload,
                        ),
                    )
                    for stage in stages
                ]
                store.replace_stages(
                    record.run_id,
                    scenario=scenario_name,
                    stages=pending_stages,
                )
                record = record.model_copy(
                    update={"scenario": scenario_name, "stages": pending_stages}
                )
            data = await RunExecutor(store, selected, stages).execute(
                record,
                context,
                resume=False,
            )
            if limit is not None:
                data.keywords = data.keywords[:limit]
            envelope = _envelope_for_research(
                data,
                context.warnings,
                context.errors,
                run_id=record.run_id,
            )
            refreshed = store.get(record.run_id)
            failed = refreshed is not None and refreshed.status is RunStatus.FAILED
            store.finish(
                record.run_id,
                status=RunStatus.FAILED if failed else RunStatus.COMPLETED,
                result=envelope.to_wire(),
                error=context.errors[0] if failed and context.errors else None,
            )
            return envelope
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
    save_run: bool = False,
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

    if save_run:
        return anyio.run(
            partial(
                _execute_saved,
                settings,
                target,
                scenario,
                market,
                seed_keyword,
                active_budget,
                limit,
            )
        )

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

    return _envelope_for_research(data, warnings, errors)
