from collections.abc import Sequence
from datetime import UTC, date, datetime

import anyio
import pytest

from google_keyword_ai.config import Settings
from google_keyword_ai.expansion import ExpansionStrategy
from google_keyword_ai.market import Market
from google_keyword_ai.normalize import KeywordCandidate
from google_keyword_ai.pipeline.budget import Budget, BudgetGuard
from google_keyword_ai.pipeline.scenarios import (
    RELEVANCE_SORT_CAVEAT,
    CompetitorResearch,
    ExistingSiteResearch,
    NewNicheResearch,
    ScenarioContext,
)
from google_keyword_ai.providers.expander import ExpansionStats
from google_keyword_ai.providers.google_ads import AdsSeed, KeywordIdea, KeywordMetrics
from google_keyword_ai.providers.search_console import (
    SearchAnalyticsPage,
    SearchAnalyticsRow,
    SiteProperty,
)
from google_keyword_ai.providers.trends.models import TrendsResult


class FakeExpander:
    def __init__(
        self,
        log: list[str],
        candidates: list[KeywordCandidate],
        stopped_by: str | None = None,
    ) -> None:
        self.log = log
        self.candidates = candidates
        self.stopped_by = stopped_by

    async def expand(
        self,
        seed: str,
        market: Market,
        *,
        strategies: Sequence[ExpansionStrategy],
    ) -> tuple[list[KeywordCandidate], ExpansionStats]:
        del seed, market, strategies
        self.log.append("autocomplete")
        return self.candidates, ExpansionStats(
            queries_executed=3, depth_reached=0, stopped_by=self.stopped_by
        )


class FakeAds:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.batches: list[list[str]] = []
        self.seeds: list[AdsSeed] = []

    async def keyword_ideas(
        self,
        seed: AdsSeed,
        market: Market,
        *,
        include_adult: bool = False,
    ) -> list[KeywordIdea]:
        del market, include_adult
        self.log.append("ads_ideas")
        self.seeds.append(seed)
        return [
            KeywordIdea(
                text="competitor keyword",
                metrics=KeywordMetrics(avg_monthly_searches=900, competition="HIGH"),
            )
        ]

    async def historical_metrics(
        self, keywords: Sequence[str], market: Market
    ) -> list[KeywordIdea]:
        del market
        batch = list(keywords)
        self.log.append("ads_historical")
        self.batches.append(batch)
        return [
            KeywordIdea(
                text=keyword,
                metrics=KeywordMetrics(avg_monthly_searches=1000 - index),
            )
            for index, keyword in enumerate(batch)
        ]


class FakeTrends:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def fetch(
        self,
        keywords: Sequence[str],
        *,
        geo: str,
        timeframe: str,
        hl: str,
    ) -> TrendsResult:
        self.log.append("trends")
        return TrendsResult(
            keywords=list(keywords),
            geo=geo,
            timeframe=timeframe,
            normalization_scope=f"{hl}-scope",
            retrieved_at=datetime.now(UTC),
            source="fake",
        )


class FakeGsc:
    def __init__(self, log: list[str]) -> None:
        self.log = log

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
    ) -> SearchAnalyticsPage:
        del (
            site_url,
            start_date,
            end_date,
            dimensions,
            market,
            search_type,
            data_state,
            row_limit,
            dimension_filters,
        )
        self.log.append("gsc")
        return SearchAnalyticsPage(
            rows=[
                SearchAnalyticsRow(
                    keys={"query": "existing keyword", "page": "/page"},
                    clicks=5,
                    impressions=500,
                    ctr=0.01,
                    position=8.0,
                )
            ],
            truncated=False,
            truncation_reason=None,
        )

    async def list_properties(self) -> list[SiteProperty]:
        self.log.append("properties")
        return [SiteProperty(site_url="https://example.com/", permission_level="siteOwner")]


def _candidates(count: int = 25) -> list[KeywordCandidate]:
    return [
        KeywordCandidate(
            raw=f"keyword {index}",
            normalized=f"keyword {index}",
            discovered_from=["autocomplete"],
            relevance=100 - index,
        )
        for index in range(count)
    ]


def _context(
    settings: Settings,
    *,
    log: list[str],
    expander: FakeExpander | None = None,
    ads: FakeAds | None = None,
    trends: FakeTrends | None = None,
    gsc: FakeGsc | None = None,
) -> ScenarioContext:
    return ScenarioContext(
        settings=settings,
        market=Market.parse("en", "US"),
        budget_guard=BudgetGuard(
            Budget(
                max_keywords=100,
                max_autocomplete_queries=50,
                max_ads_calls=5,
                max_trends_calls=2,
            )
        ),
        google_ads=ads,
        trends=trends,
        search_console=gsc,
        expander=expander,
        availability={
            "autocomplete": expander is not None,
            "google_ads": ads is not None,
            "trends": trends is not None,
            "search_console": gsc is not None,
        },
    )


def test_niche_cheap_first_filters_and_batches_after_deduplication(settings: Settings) -> None:
    async def exercise() -> None:
        log: list[str] = []
        candidates = _candidates()
        candidates.extend(
            [
                candidates[0].model_copy(),
                KeywordCandidate(raw="x", normalized="x", discovered_from=["autocomplete"]),
                KeywordCandidate(raw="seed", normalized="seed", discovered_from=["autocomplete"]),
            ]
        )
        ads = FakeAds(log)
        context = _context(
            settings,
            log=log,
            expander=FakeExpander(log, candidates),
            ads=ads,
            trends=FakeTrends(log),
        )

        data = await NewNicheResearch("seed").run(context)

        assert log == ["autocomplete", "ads_historical", "ads_historical", "trends"]
        assert [len(batch) for batch in ads.batches] == [20, 5]
        assert "x" not in {keyword for batch in ads.batches for keyword in batch}
        assert "seed" not in {keyword for batch in ads.batches for keyword in batch}
        assert len(data.keywords) == 25

    anyio.run(exercise)


def test_competitor_order_and_site_seed(settings: Settings) -> None:
    async def exercise() -> None:
        log: list[str] = []
        ads = FakeAds(log)
        data = await CompetitorResearch("example.com").run(
            _context(settings, log=log, ads=ads, trends=FakeTrends(log))
        )
        assert log == ["ads_ideas", "trends"]
        assert ads.seeds[0].mode() == "site_seed"
        assert data.keywords[0].keyword == "competitor keyword"

    anyio.run(exercise)


def test_existing_site_order_and_ads_batch(settings: Settings) -> None:
    async def exercise() -> None:
        log: list[str] = []
        ads = FakeAds(log)
        data = await ExistingSiteResearch("https://example.com/").run(
            _context(settings, log=log, ads=ads, trends=FakeTrends(log), gsc=FakeGsc(log))
        )
        assert log == ["gsc", "ads_historical", "trends"]
        assert ads.batches == [["existing keyword"]]
        assert data.keywords[0].gsc_impressions == 500

    anyio.run(exercise)


def test_unavailable_ads_does_not_break_any_scenario(settings: Settings) -> None:
    async def exercise() -> None:
        niche_log: list[str] = []
        niche_context = _context(
            settings,
            log=niche_log,
            expander=FakeExpander(niche_log, _candidates(2)),
        )
        niche = await NewNicheResearch("seed").run(niche_context)
        assert niche.keywords
        assert niche_context.warnings

        competitor_context = _context(settings, log=[])
        competitor = await CompetitorResearch("example.com").run(competitor_context)
        assert competitor.keywords == []
        assert any("no fallback" in warning for warning in competitor_context.warnings)

        site_log: list[str] = []
        site_context = _context(settings, log=site_log, gsc=FakeGsc(site_log))
        site = await ExistingSiteResearch("https://example.com/").run(site_context)
        assert site.keywords
        assert site_context.warnings

    anyio.run(exercise)


def test_sorting_falls_back_to_autocomplete_relevance_and_records_caveat(
    settings: Settings,
) -> None:
    async def exercise() -> None:
        log: list[str] = []
        candidates = _candidates(2)
        candidates[0].relevance = 1
        candidates[1].relevance = 100
        data = await NewNicheResearch("seed").run(
            _context(settings, log=log, expander=FakeExpander(log, candidates))
        )
        assert [keyword.autocomplete_relevance for keyword in data.keywords] == [100, 1]
        assert RELEVANCE_SORT_CAVEAT in data.data_quality.caveats

    anyio.run(exercise)


def test_ads_candidate_pool_is_capped_by_the_call_budget(settings: Settings) -> None:
    """The cap is the point of cheap-first, and a wide budget hides its absence.

    With the default allowance the ceiling of ``max_ads_calls * 20`` sits far
    above any realistic candidate list, so removing it changes nothing and the
    existing test stays green. Squeeze the budget until the ceiling binds.
    """

    async def exercise() -> None:
        log: list[str] = []
        ads = FakeAds(log)
        context = _context(
            settings,
            log=log,
            expander=FakeExpander(log, _candidates(count=90)),
            ads=ads,
        )
        context.budget_guard.budget.max_ads_calls = 2

        await NewNicheResearch("seed").run(context)

        sent = [keyword for batch in ads.batches for keyword in batch]
        assert len(sent) == 40, "only max_ads_calls * 20 candidates may reach Google Ads"
        assert [len(batch) for batch in ads.batches] == [20, 20]

    anyio.run(exercise)


def test_relevance_fallback_is_written_into_the_caveats(settings: Settings) -> None:
    """A flag nobody can read is not a warning.

    Sorting by Autocomplete relevance instead of search volume changes what the
    numbers mean, so the reason has to reach the report, not just an internal
    boolean.
    """

    async def exercise() -> None:
        log: list[str] = []
        context = _context(settings, log=log, expander=FakeExpander(log, _candidates(count=3)))

        data = await NewNicheResearch("seed").run(context)

        assert RELEVANCE_SORT_CAVEAT
        assert RELEVANCE_SORT_CAVEAT in data.data_quality.caveats

    anyio.run(exercise)


@pytest.mark.parametrize(
    ("expansion_stop", "expected"),
    [
        ("max_depth", None),
        ("max_queries", "max_autocomplete_queries"),
        ("max_results", "max_keywords"),
        ("max_runtime", "max_runtime_seconds"),
    ],
)
def test_only_budget_stops_mark_a_research_run_cut_short(
    settings: Settings, expansion_stop: str, expected: str | None
) -> None:
    """Reaching the requested fan-out depth is an ordinary finish, not a budget cut.

    Propagating it made every healthy run partial, so `gkai research` and
    `gkai run resume` exited 1 on success. docs/expansion.md states the contract.
    """

    async def exercise() -> None:
        log: list[str] = []
        context = _context(
            settings,
            log=log,
            expander=FakeExpander(log, _candidates(), stopped_by=expansion_stop),
        )

        data = await NewNicheResearch("seed").run(context)

        assert data.stats.stopped_by == expected

    anyio.run(exercise)
