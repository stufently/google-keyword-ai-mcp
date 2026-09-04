from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol, cast

from google_keyword_ai.config import Settings
from google_keyword_ai.errors import GkaiError
from google_keyword_ai.expansion import ExpansionStrategy
from google_keyword_ai.market import Market
from google_keyword_ai.normalize import KeywordCandidate, deduplicate, normalize_keyword
from google_keyword_ai.opportunities import Opportunity, find_opportunities
from google_keyword_ai.pipeline.budget import BudgetGuard
from google_keyword_ai.pipeline.models import (
    DataQuality,
    DryRunPlan,
    ResearchData,
    ResearchKeyword,
    ResearchStats,
    SourceUsage,
)
from google_keyword_ai.providers.expander import ExpansionStats
from google_keyword_ai.providers.google_ads import AdsSeed, KeywordIdea
from google_keyword_ai.providers.search_console import SearchAnalyticsPage, SiteProperty
from google_keyword_ai.providers.trends.models import TrendsResult
from google_keyword_ai.targets import is_bare_domain

# The three standing prohibitions, in the language the rest of the output uses.
# Expansion budget names, translated to the research budget option that caused
# the stop. "max_depth" is deliberately absent: reaching the requested fan-out
# depth is the ordinary end of expansion, not a budget cut, and propagating it
# would mark every healthy run partial and make the exit code useless. This is
# the contract documented in docs/expansion.md and already honoured by
# usecases/expand.py.
_EXPANSION_STOP_TO_BUDGET = {
    "max_queries": "max_autocomplete_queries",
    "max_results": "max_keywords",
    "max_runtime": "max_runtime_seconds",
}

TRENDS_CAVEAT = "Google Trends values are relative interest on a 0-100 scale, not search volume."
ADS_CAVEAT = "Google Ads competition describes advertiser demand, not SEO difficulty."
SITE_SEED_CAVEAT = (
    "A site seed returns keyword ideas Google associates with the site, "
    "not the queries the site actually ranks for."
)
RELEVANCE_SORT_CAVEAT = (
    "Google Ads metrics are absent; keywords are sorted by Autocomplete relevance."
)
GENERAL_CAVEAT = "Metrics are source-specific; missing values were not imputed."


class ExpanderLike(Protocol):
    async def expand(
        self,
        seed: str,
        market: Market,
        *,
        strategies: Sequence[ExpansionStrategy],
    ) -> tuple[list[KeywordCandidate], ExpansionStats]: ...


class AdsLike(Protocol):
    async def keyword_ideas(
        self, seed: AdsSeed, market: Market, *, include_adult: bool = False
    ) -> list[KeywordIdea]: ...

    async def historical_metrics(
        self, keywords: Sequence[str], market: Market
    ) -> list[KeywordIdea]: ...


class TrendsLike(Protocol):
    async def fetch(
        self, keywords: Sequence[str], *, geo: str, timeframe: str, hl: str
    ) -> TrendsResult: ...


class SearchConsoleLike(Protocol):
    async def query(
        self,
        site_url: str,
        *,
        start_date: date | str,
        end_date: date | str,
        dimensions: Sequence[str],
        market: Market | None = None,
        search_type: str = "web",
        data_state: str = "full",
        row_limit: int | None = None,
        dimension_filters: Sequence[dict[str, str]] | None = None,
    ) -> SearchAnalyticsPage: ...

    async def list_properties(self) -> list[SiteProperty]: ...


@dataclass
class ScenarioContext:
    settings: Settings
    market: Market
    budget_guard: BudgetGuard
    autocomplete: object | None = None
    google_ads: AdsLike | None = None
    trends: TrendsLike | None = None
    search_console: SearchConsoleLike | None = None
    expander: ExpanderLike | None = None
    availability: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def available(self, name: str) -> bool:
        if name in self.availability:
            return self.availability[name]
        return {
            "autocomplete": self.expander is not None,
            "google_ads": self.google_ads is not None,
            "trends": self.trends is not None,
            "search_console": self.search_console is not None,
        }[name]


def _source(name: str, context: ScenarioContext, *, used: bool, detail: str) -> SourceUsage:
    return SourceUsage(
        name=name,
        used=used,
        available=context.available(name),
        detail=detail,
    )


def _sources(
    context: ScenarioContext,
    used: set[str],
    details: dict[str, str] | None = None,
) -> list[SourceUsage]:
    source_details = {} if details is None else details
    return [
        _source(
            name,
            context,
            used=name in used,
            detail=source_details.get(
                name,
                (
                    "used"
                    if name in used
                    else ("available" if context.available(name) else "unavailable")
                ),
            ),
        )
        for name in ("autocomplete", "google_ads", "trends", "search_console")
    ]


def _quality(
    context: ScenarioContext,
    used: set[str],
    *,
    site_seed: bool = False,
    relevance_fallback: bool = False,
    details: dict[str, str] | None = None,
) -> DataQuality:
    caveats: list[str] = [GENERAL_CAVEAT]
    if "trends" in used:
        caveats.append(TRENDS_CAVEAT)
    if "google_ads" in used:
        caveats.append(ADS_CAVEAT)
    if site_seed and "google_ads" in used:
        caveats.append(SITE_SEED_CAVEAT)
    if relevance_fallback:
        caveats.append(RELEVANCE_SORT_CAVEAT)
    absolute = []
    relative = []
    derived = []
    if "google_ads" in used:
        absolute.extend(["avg_monthly_searches", "low_top_of_page_bid", "high_top_of_page_bid"])
        relative.extend(["ads_competition", "ads_competition_index"])
    if "search_console" in used:
        absolute.extend(["gsc_impressions", "gsc_clicks"])
        relative.extend(["gsc_ctr", "gsc_position"])
        derived.append("opportunities")
    if "trends" in used:
        relative.append("trends_0_100")
    return DataQuality(
        sources=_sources(context, used, details),
        retrieved_at=datetime.now(UTC),
        absolute_metrics=absolute,
        relative_metrics=relative,
        derived_metrics=derived,
        caveats=caveats,
    )


def _apply_ideas(keywords: list[ResearchKeyword], ideas: Sequence[KeywordIdea]) -> None:
    by_normalized = {keyword.normalized: keyword for keyword in keywords}
    for idea in ideas:
        normalized = normalize_keyword(idea.text)
        keyword = by_normalized.get(normalized)
        if keyword is None:
            keyword = ResearchKeyword(
                keyword=idea.text,
                normalized=normalized,
                discovered_from=["google_ads"],
            )
            keywords.append(keyword)
            by_normalized[normalized] = keyword
        elif "google_ads" not in keyword.discovered_from:
            keyword.discovered_from.append("google_ads")
        metrics = idea.metrics
        keyword.avg_monthly_searches = metrics.avg_monthly_searches
        keyword.ads_competition = metrics.competition
        keyword.ads_competition_index = metrics.competition_index
        keyword.low_top_of_page_bid = metrics.low_top_of_page_bid
        keyword.high_top_of_page_bid = metrics.high_top_of_page_bid


def _sort_keywords(keywords: list[ResearchKeyword]) -> bool:
    has_ads = any(keyword.avg_monthly_searches is not None for keyword in keywords)
    if has_ads:
        keywords.sort(
            key=lambda keyword: (
                keyword.avg_monthly_searches is None,
                -(keyword.avg_monthly_searches or 0),
                keyword.normalized,
            )
        )
        return False
    keywords.sort(
        key=lambda keyword: (
            keyword.autocomplete_relevance is None,
            -(keyword.autocomplete_relevance or 0),
            keyword.normalized,
        )
    )
    return bool(keywords)


def _note_expansion(context: ScenarioContext, expansion: ExpansionStats, found: bool) -> None:
    """Say what the fan-out did, at the moment it did it.

    Both of these used to be reported out of order: the failure count was
    appended during the final assembly, after every later source had already
    warned. An empty envelope names its first warning as the cause on the
    understanding that warnings accumulate as the run proceeds, so a warning
    about the very first step arriving last made a failing Autocomplete read as
    an absent Google Ads -- and made a fan-out that partly failed read as one
    that succeeded and found nothing.
    """
    if expansion.queries_failed:
        # Skipping a dead request keeps the fan-out alive, but the keywords it
        # would have found are simply absent. Without this the result reads as
        # a small niche rather than a source that was failing.
        context.warnings.append(
            f"{expansion.queries_failed} of {expansion.queries_executed} Autocomplete requests "
            "failed and were skipped; the keywords they would have returned are missing."
        )
    if not found:
        context.warnings.append("Autocomplete returned no usable keywords for this seed.")


async def _fetch_trends(
    context: ScenarioContext, keyword: str | None, used: set[str]
) -> TrendsResult | None:
    if keyword is None or context.trends is None or not context.budget_guard.can_spend("trends"):
        return None
    context.budget_guard.spend("trends")
    used.add("trends")
    try:
        result = await context.trends.fetch(
            [keyword],
            geo=context.market.trends_geo(),
            timeframe="today 12-m",
            hl=context.market.language,
        )
        context.warnings.extend(cast(list[str], getattr(context.trends, "warnings", [])))
        return result
    except GkaiError as exc:
        context.errors.append(str(exc))
        return None


async def _enrich_ads(
    context: ScenarioContext,
    keywords: list[ResearchKeyword],
    selected: Sequence[ResearchKeyword],
    used: set[str],
) -> None:
    if context.google_ads is None:
        context.warnings.append("Google Ads is unavailable; absolute search metrics are omitted.")
        return
    # Trimming here rather than at the call site keeps the caller free to hand
    # over its whole ranked list, and puts the cut behind the availability
    # check: a budget that never got the chance to spend anything has not cut
    # anything either. The loop below still records a refusal, which is what
    # catches the runtime ceiling and any ads calls already spent.
    selected = _cap(
        context,
        list(selected),
        context.budget_guard.budget.max_ads_calls * 20,
        kind="ads",
    )
    for start in range(0, len(selected), 20):
        batch = selected[start : start + 20]
        if not batch or not context.budget_guard.can_spend("ads"):
            break
        context.budget_guard.spend("ads")
        used.add("google_ads")
        try:
            ideas = await context.google_ads.historical_metrics(
                [keyword.keyword for keyword in batch], context.market
            )
        except GkaiError as exc:
            context.errors.append(str(exc))
            break
        _apply_ideas(keywords, ideas)


def _cap[T](
    context: ScenarioContext,
    items: list[T],
    limit: int,
    *,
    kind: str = "keywords",
) -> list[T]:
    """Trim a list to a budget limit, recording it as a cut when it bites.

    `kind` names the budget doing the trimming, because that is what the
    result should report as the reason it stopped short.
    """
    if len(items) > limit:
        context.budget_guard.mark_cut(kind)
    return items[:limit]


def _research_data(
    context: ScenarioContext,
    *,
    scenario: str,
    input_value: str,
    keywords: list[ResearchKeyword],
    used: set[str],
    expansion: ExpansionStats | None = None,
    trends: TrendsResult | None = None,
    opportunities: list[Opportunity] | None = None,
    site_seed: bool = False,
    details: dict[str, str] | None = None,
) -> ResearchData:
    relevance_fallback = _sort_keywords(keywords)
    stopped_by = context.budget_guard.exhausted_reason()
    if expansion is not None and expansion.stopped_by is not None:
        stopped_by = _EXPANSION_STOP_TO_BUDGET.get(expansion.stopped_by, stopped_by)
    return ResearchData(
        scenario=scenario,
        input=input_value,
        language=context.market.language,
        country=context.market.country,
        keywords=keywords,
        trends=trends,
        opportunities=[] if opportunities is None else opportunities,
        stats=ResearchStats(
            expansion=expansion,
            spend=context.budget_guard.spend.model_copy(deep=True),
            stopped_by=stopped_by,
        ),
        data_quality=_quality(
            context,
            used,
            site_seed=site_seed,
            relevance_fallback=relevance_fallback,
            details=details,
        ),
    )


class NewNicheResearch:
    def __init__(self, seed: str) -> None:
        self.seed = seed

    async def run(self, context: ScenarioContext) -> ResearchData:
        used: set[str] = set()
        expansion: ExpansionStats | None = None
        candidates: list[KeywordCandidate] = []
        if context.expander is None:
            context.warnings.append("Autocomplete is unavailable; niche expansion was skipped.")
        else:
            used.add("autocomplete")
            try:
                candidates, expansion = await context.expander.expand(
                    self.seed,
                    context.market,
                    strategies=list(ExpansionStrategy),
                )
            except GkaiError as exc:
                context.errors.append(str(exc))
            if expansion is not None:
                query_spend = min(
                    expansion.queries_executed,
                    context.budget_guard.budget.max_autocomplete_queries,
                )
                if query_spend:
                    context.budget_guard.spend("autocomplete", query_spend)

        seed_normalized = normalize_keyword(self.seed)
        filtered = _cap(
            context,
            [
                candidate
                for candidate in deduplicate(candidates)
                if candidate.normalized
                and len(candidate.normalized) > 1
                and candidate.normalized != seed_normalized
            ],
            context.budget_guard.budget.max_keywords,
        )
        if filtered:
            context.budget_guard.spend("keywords", len(filtered))
        if expansion is not None:
            _note_expansion(context, expansion, found=bool(filtered))
        keywords = [
            ResearchKeyword(
                keyword=candidate.raw,
                normalized=candidate.normalized,
                discovered_from=candidate.discovered_from,
                autocomplete_relevance=candidate.relevance,
            )
            for candidate in filtered
        ]
        # Ranked, not trimmed: `_enrich_ads` applies the ads budget itself, so
        # that an unavailable provider cannot be reported as a budget cut.
        selected = sorted(
            keywords,
            key=lambda keyword: (
                keyword.autocomplete_relevance is None,
                -(keyword.autocomplete_relevance or 0),
            ),
        )
        await _enrich_ads(context, keywords, selected, used)
        trends = await _fetch_trends(context, self.seed, used)
        return _research_data(
            context,
            scenario="niche",
            input_value=self.seed,
            keywords=keywords,
            used=used,
            expansion=expansion,
            trends=trends,
        )

    def plan(self, context: ScenarioContext) -> DryRunPlan:
        budget = context.budget_guard.budget
        return DryRunPlan(
            scenario="niche",
            steps=[
                "Expand with Autocomplete within the query and keyword budgets",
                "Deduplicate and filter candidates",
                "Fetch Google Ads historical metrics in batches of 20",
                "Fetch Google Trends for the seed",
            ],
            estimated_autocomplete_queries=budget.max_autocomplete_queries,
            estimated_ads_calls=min(budget.max_ads_calls, (budget.max_keywords + 19) // 20),
            estimated_trends_calls=min(1, budget.max_trends_calls),
            sources=_sources(context, set()),
        )


class CompetitorResearch:
    def __init__(self, target: str, seed_keyword: str | None = None) -> None:
        self.target = target
        self.seed_keyword = seed_keyword

    async def run(self, context: ScenarioContext) -> ResearchData:
        used: set[str] = set()
        keywords: list[ResearchKeyword] = []
        expansion: ExpansionStats | None = None
        site_seed = self.seed_keyword is None and is_bare_domain(self.target)
        ads_available = context.google_ads is not None
        if context.google_ads is not None and context.budget_guard.can_spend("ads"):
            if self.seed_keyword is not None:
                seed = AdsSeed(keywords=[self.seed_keyword], url=self.target)
            elif site_seed:
                seed = AdsSeed(site=self.target)
            else:
                seed = AdsSeed(url=self.target)
            context.budget_guard.spend("ads")
            used.add("google_ads")
            try:
                ideas = await context.google_ads.keyword_ideas(seed, context.market)
            except GkaiError as exc:
                context.errors.append(str(exc))
            else:
                _apply_ideas(
                    keywords,
                    _cap(context, list(ideas), context.budget_guard.budget.max_keywords),
                )
                if keywords:
                    context.budget_guard.spend("keywords", len(keywords))
                else:
                    # The call succeeded and returned nothing. Without this the
                    # run is empty for no stated reason at all, because every
                    # warning here describes a source that was missing.
                    context.warnings.append("Google Ads returned no keyword ideas for this target.")
        else:
            out_of_time = context.budget_guard.exhausted_reason() == "max_runtime_seconds"
            # Three different things end here and they call for three different
            # actions: a missing credential, a number the caller chose, and a
            # clock that ran out.
            if not ads_available:
                context.warnings.append(
                    "Google Ads is unavailable; site-based keyword ideas are unavailable."
                )
            elif out_of_time:
                context.warnings.append(
                    "The run ran out of time before site-based keyword ideas could be requested."
                )
            else:
                context.warnings.append(
                    "The Google Ads budget was spent before site-based keyword ideas "
                    "could be requested."
                )
            if out_of_time:
                # The expander keeps its own clock and starts it fresh on every
                # call, so a fallback launched past the ceiling would spend the
                # budget's whole runtime allowance a second time.
                context.warnings.append(
                    "No fallback expansion was attempted, because the run was already out of time."
                )
            elif self.seed_keyword is None:
                context.warnings.append(
                    "No seed keyword was provided, so no fallback expansion is possible."
                )
            elif context.expander is None:
                # Without this the run reports nothing at all and never says
                # why: both sources were gone, and only one of them said so.
                context.warnings.append(
                    "Autocomplete is unavailable, so no fallback expansion is possible."
                )
            else:
                used.add("autocomplete")
                try:
                    candidates, expansion = await context.expander.expand(
                        self.seed_keyword,
                        context.market,
                        strategies=list(ExpansionStrategy),
                    )
                except GkaiError as exc:
                    context.errors.append(str(exc))
                else:
                    filtered = _cap(
                        context,
                        deduplicate(candidates),
                        context.budget_guard.budget.max_keywords,
                    )
                    keywords = [
                        ResearchKeyword(
                            keyword=candidate.raw,
                            normalized=candidate.normalized,
                            discovered_from=candidate.discovered_from,
                            autocomplete_relevance=candidate.relevance,
                        )
                        for candidate in filtered
                        if candidate.normalized and len(candidate.normalized) > 1
                    ]
                    if keywords:
                        context.budget_guard.spend("keywords", len(keywords))
                    if expansion.queries_executed:
                        context.budget_guard.spend(
                            "autocomplete",
                            min(
                                expansion.queries_executed,
                                context.budget_guard.budget.max_autocomplete_queries,
                            ),
                        )
                    _note_expansion(context, expansion, found=bool(keywords))
        notable = (
            max(keywords, key=lambda keyword: keyword.avg_monthly_searches or 0).keyword
            if keywords
            else self.seed_keyword
        )
        trends = await _fetch_trends(context, notable, used)
        return _research_data(
            context,
            scenario="competitor",
            input_value=self.target,
            keywords=keywords,
            used=used,
            expansion=expansion,
            trends=trends,
            site_seed=site_seed,
        )

    def plan(self, context: ScenarioContext) -> DryRunPlan:
        budget = context.budget_guard.budget
        return DryRunPlan(
            scenario="competitor",
            steps=[
                "Request site or URL keyword ideas from Google Ads",
                "Fall back to Autocomplete expansion when a seed keyword is available",
                "Fetch Google Trends for the most notable keyword",
            ],
            estimated_autocomplete_queries=(
                budget.max_autocomplete_queries if self.seed_keyword is not None else 0
            ),
            estimated_ads_calls=min(1, budget.max_ads_calls),
            estimated_trends_calls=min(1, budget.max_trends_calls),
            sources=_sources(context, set()),
        )


class ExistingSiteResearch:
    def __init__(self, site_url: str) -> None:
        self.site_url = site_url

    async def run(self, context: ScenarioContext) -> ResearchData:
        used: set[str] = set()
        opportunities: list[Opportunity] = []
        keywords: list[ResearchKeyword] = []
        if context.search_console is None:
            context.warnings.append("Search Console is unavailable; site queries cannot be read.")
        else:
            used.add("search_console")
            end_date = datetime.now(UTC).date() - timedelta(days=2)
            start_date = end_date - timedelta(days=27)
            try:
                page = await context.search_console.query(
                    self.site_url,
                    start_date=start_date,
                    end_date=end_date,
                    dimensions=["query", "page"],
                    market=context.market,
                )
            except GkaiError as exc:
                context.errors.append(str(exc))
            else:
                opportunities = find_opportunities(page.rows, context.settings)
                if not page.rows:
                    # Every keyword here grows out of an opportunity, so an
                    # empty window leaves nothing behind and no trace of why.
                    # The reason then falls through to whatever else warned --
                    # usually an absent Google Ads, which had nothing to do
                    # with it.
                    context.warnings.append(
                        "Search Console returned no rows for the requested window."
                    )
                elif not opportunities:
                    # Every keyword this scenario returns comes from an
                    # opportunity, so a site whose rows all sit outside the
                    # position window produces nothing -- and without this the
                    # run reads exactly like a property Search Console had no
                    # data for. Those are opposite findings: one says widen the
                    # thresholds, the other says check the property.
                    context.warnings.append(
                        f"{len(page.rows)} Search Console rows were read and none met the "
                        "opportunity thresholds, so no keywords were derived from them."
                    )
                for opportunity in _cap(
                    context, opportunities, context.budget_guard.budget.max_keywords
                ):
                    keywords.append(
                        ResearchKeyword(
                            keyword=opportunity.query,
                            normalized=normalize_keyword(opportunity.query),
                            discovered_from=["search_console"],
                            gsc_impressions=opportunity.impressions,
                            gsc_clicks=opportunity.clicks,
                            gsc_ctr=opportunity.ctr,
                            gsc_position=opportunity.position,
                        )
                    )
                if keywords:
                    context.budget_guard.spend("keywords", len(keywords))
                if page.truncated and page.truncation_reason:
                    context.warnings.append(page.truncation_reason)
        await _enrich_ads(context, keywords, keywords, used)
        frequent = (
            max(keywords, key=lambda keyword: keyword.gsc_impressions or 0).keyword
            if keywords
            else None
        )
        trends = await _fetch_trends(context, frequent, used)
        return _research_data(
            context,
            scenario="site",
            input_value=self.site_url,
            keywords=keywords,
            used=used,
            trends=trends,
            opportunities=opportunities,
        )

    def plan(self, context: ScenarioContext) -> DryRunPlan:
        budget = context.budget_guard.budget
        return DryRunPlan(
            scenario="site",
            steps=[
                "Read query and page rows from Search Console",
                "Derive opportunities",
                "Fetch Google Ads historical metrics in batches of 20",
                "Fetch Google Trends for the highest-impression query",
            ],
            estimated_autocomplete_queries=0,
            estimated_ads_calls=min(budget.max_ads_calls, (budget.max_keywords + 19) // 20),
            estimated_trends_calls=min(1, budget.max_trends_calls),
            sources=_sources(context, set()),
        )
