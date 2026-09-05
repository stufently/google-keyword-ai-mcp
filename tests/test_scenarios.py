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
from google_keyword_ai.providers.google_ads import (
    AdsSeed,
    KeywordIdea,
    KeywordIdeaPage,
    KeywordMetrics,
)
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
        queries_failed: int = 0,
    ) -> None:
        self.log = log
        self.candidates = candidates
        self.stopped_by = stopped_by
        self.queries_failed = queries_failed

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
            queries_executed=3,
            depth_reached=0,
            stopped_by=self.stopped_by,
            queries_failed=self.queries_failed,
        )


class FakeAds:
    def __init__(
        self,
        log: list[str],
        ideas: list[KeywordIdea] | None = None,
        *,
        truncated: bool = False,
        truncation_reason: str | None = None,
    ) -> None:
        self.log = log
        self.batches: list[list[str]] = []
        self.seeds: list[AdsSeed] = []
        self.ideas = ideas
        self.truncated = truncated
        self.truncation_reason = truncation_reason

    async def keyword_ideas(
        self,
        seed: AdsSeed,
        market: Market,
        *,
        include_adult: bool = False,
    ) -> KeywordIdeaPage:
        del market, include_adult
        self.log.append("ads_ideas")
        self.seeds.append(seed)
        ideas = (
            self.ideas
            if self.ideas is not None
            else [
                KeywordIdea(
                    text="competitor keyword",
                    metrics=KeywordMetrics(avg_monthly_searches=900, competition="HIGH"),
                )
            ]
        )
        return KeywordIdeaPage(
            ideas=ideas,
            truncated=self.truncated,
            truncation_reason=self.truncation_reason,
        )

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
    def __init__(self, log: list[str], rows: list[SearchAnalyticsRow] | None = None) -> None:
        self.log = log
        self.rows = rows

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
        default = [
            SearchAnalyticsRow(
                keys={"query": "existing keyword", "page": "/page"},
                clicks=5,
                impressions=500,
                ctr=0.01,
                position=8.0,
            )
        ]
        return SearchAnalyticsPage(
            rows=default if self.rows is None else self.rows,
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
    budget: Budget | None = None,
) -> ScenarioContext:
    return ScenarioContext(
        settings=settings,
        market=Market.parse("en", "US"),
        budget_guard=BudgetGuard(
            budget
            or Budget(
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


def test_competitor_research_keeps_keywords_when_ads_ideas_truncated(
    settings: Settings,
) -> None:
    async def exercise() -> None:
        log: list[str] = []
        ads = FakeAds(
            log,
            ideas=[
                KeywordIdea(
                    text="kept keyword",
                    metrics=KeywordMetrics(avg_monthly_searches=10, competition="LOW"),
                )
            ],
            truncated=True,
            truncation_reason="Keyword ideas were truncated by google_ads_max_pages.",
        )
        context = _context(settings, log=log, ads=ads, trends=FakeTrends(log))
        data = await CompetitorResearch("example.com").run(context)
        assert any(
            "google_ads_max_pages" in warning or "truncat" in warning.lower()
            for warning in context.warnings
        )
        assert any(keyword.keyword == "kept keyword" for keyword in data.keywords)

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


@pytest.mark.parametrize(
    ("candidates", "expected"),
    [(40, None), (90, "max_ads_calls")],
)
def test_the_ads_call_budget_is_a_cut_only_when_it_leaves_keywords_unpriced(
    settings: Settings,
    candidates: int,
    expected: str | None,
) -> None:
    """Counting calls is not the same as noticing that keywords went unpriced.

    The candidate pool is trimmed to ``max_ads_calls * 20`` before the enrichment
    loop, so the loop is never refused a batch and cannot record the stop itself.
    Spending the whole allowance on a pool that fits is complete; leaving fifty
    keywords without metrics is not, and only the reason tells them apart.
    """

    async def exercise() -> None:
        log: list[str] = []
        ads = FakeAds(log)
        context = _context(
            settings,
            log=log,
            expander=FakeExpander(log, _candidates(count=candidates)),
            ads=ads,
        )
        context.budget_guard.budget.max_ads_calls = 2

        data = await NewNicheResearch("seed").run(context)

        assert [len(batch) for batch in ads.batches] == [20, 20]
        assert data.stats.stopped_by == expected

    anyio.run(exercise)


def test_an_unavailable_ads_provider_is_not_a_budget_cut(settings: Settings) -> None:
    """A budget that never got the chance to spend anything has cut nothing.

    Without credentials the enrichment returns before making a single call, so
    naming ``max_ads_calls`` as the reason the run stopped short points at the
    wrong cause: the warning about the missing provider is the real one, and it
    already makes the result partial.
    """

    async def exercise() -> None:
        log: list[str] = []
        context = _context(
            settings,
            log=log,
            expander=FakeExpander(log, _candidates(count=90)),
        )
        context.budget_guard.budget.max_ads_calls = 2

        data = await NewNicheResearch("seed").run(context)

        assert data.stats.stopped_by is None
        assert data.stats.spend.ads_calls == 0

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


@pytest.mark.parametrize(
    ("candidate_count", "expected"),
    [(10, None), (25, "max_keywords")],
)
def test_a_keyword_budget_is_a_cut_only_when_it_actually_truncates(
    settings: Settings, candidate_count: int, expected: str | None
) -> None:
    """Fitting inside the allowance exactly is a complete result.

    The guard used to answer "exhausted" as soon as a counter equalled its
    limit, so research that produced exactly `max_keywords` keywords -- having
    lost nothing -- was reported as `partial / stopped by max_keywords`.
    Truncation is now recorded where it happens, so both directions are honest.
    """

    async def exercise() -> None:
        log: list[str] = []
        context = _context(
            settings,
            log=log,
            expander=FakeExpander(log, _candidates(candidate_count)),
            budget=Budget(
                max_keywords=10,
                max_autocomplete_queries=50,
                max_ads_calls=5,
                max_trends_calls=2,
            ),
        )

        data = await NewNicheResearch("seed").run(context)

        assert len(data.keywords) == 10
        assert data.stats.stopped_by == expected

    anyio.run(exercise)


def test_skipped_autocomplete_requests_reach_the_research_warnings(settings: Settings) -> None:
    """A research run has to inherit what the expansion quietly survived.

    The expander skips a failed request on purpose, but the keywords it would
    have found are missing all the same. Without the count the run reports a
    healthy, merely small result.
    """

    async def exercise() -> None:
        log: list[str] = []
        context = _context(
            settings,
            log=log,
            expander=FakeExpander(log, _candidates(2), queries_failed=2),
        )

        await NewNicheResearch("seed").run(context)

        assert any("2 of 3 Autocomplete requests failed" in warning for warning in context.warnings)

    anyio.run(exercise)


def test_site_rows_that_met_no_threshold_say_so(settings: Settings) -> None:
    """A site with traffic and no opportunities has to say which of the two it is.

    Every keyword the site scenario returns comes from an opportunity, so a site
    whose rows all sit outside the position window produces nothing — and the
    run then reads exactly like a property Search Console had no data for at
    all. Those are opposite findings: one says widen the thresholds, the other
    says check the property.
    """

    async def exercise() -> None:
        log: list[str] = []
        rows = [
            SearchAnalyticsRow(
                keys={"query": "brand", "page": "/"},
                clicks=900,
                impressions=1000,
                ctr=0.9,
                position=1.1,
            )
        ]
        context = _context(settings, log=log, gsc=FakeGsc(log, rows=rows))
        data = await ExistingSiteResearch("https://example.com/").run(context)

        assert data.keywords == []
        assert any("threshold" in warning for warning in context.warnings), context.warnings

    anyio.run(exercise)


def test_competitor_falls_back_to_expansion_when_ads_is_gone(settings: Settings) -> None:
    """With a seed keyword and no Google Ads, Autocomplete is the fallback.

    This whole branch was unexercised: the competitor scenario is the one that
    normally never touches Autocomplete, so nothing pinned what it does when the
    only paid source is missing and a seed was supplied to work from.
    """

    async def exercise() -> None:
        log: list[str] = []
        expander = FakeExpander(log, _candidates(3))
        context = _context(settings, log=log, expander=expander)
        data = await CompetitorResearch("example.com", seed_keyword="seed").run(context)

        assert [keyword.keyword for keyword in data.keywords] == [
            candidate.raw for candidate in _candidates(3)
        ]
        assert context.budget_guard.spend.autocomplete_queries > 0
        assert any("Google Ads is unavailable" in warning for warning in context.warnings)

    anyio.run(exercise)


def test_competitor_says_which_source_was_missing(settings: Settings) -> None:
    """An empty answer names what produced it, and there are three ways here.

    Ads gone with no seed, ads gone with a seed but no Autocomplete to expand
    it, and ads present but out of budget. The middle one used to warn about
    nothing at all, and the last one blamed an unavailable provider for a number
    the caller chose.
    """

    async def exercise() -> None:
        no_seed = _context(settings, log=[])
        await CompetitorResearch("example.com").run(no_seed)
        assert any("no fallback" in warning for warning in no_seed.warnings)

        no_expander = _context(settings, log=[])
        await CompetitorResearch("example.com", seed_keyword="seed").run(no_expander)
        assert any("Autocomplete is unavailable" in w for w in no_expander.warnings), (
            no_expander.warnings
        )

        log: list[str] = []
        spent = _context(
            settings,
            log=log,
            ads=FakeAds(log),
            budget=Budget(max_ads_calls=1, max_keywords=10, max_trends_calls=1),
        )
        spent.budget_guard.spend("ads")
        await CompetitorResearch("example.com").run(spent)
        assert any("budget was spent" in warning for warning in spent.warnings), spent.warnings
        assert not any("Google Ads is unavailable" in w for w in spent.warnings)

    anyio.run(exercise)


def test_an_answer_of_nothing_is_never_left_unexplained(settings: Settings) -> None:
    """Every source that succeeded and returned nothing has to say so itself.

    Otherwise the empty run is explained by whatever warning happens to be
    nearby — normally an absent Google Ads, which in three of these four cases
    had nothing to do with it.
    """

    async def exercise() -> None:
        ads_log: list[str] = []
        ads_context = _context(settings, log=ads_log, ads=FakeAds(ads_log, ideas=[]))
        ads_data = await CompetitorResearch("example.com").run(ads_context)
        assert ads_data.keywords == []
        assert any("returned no keyword ideas" in w for w in ads_context.warnings), (
            ads_context.warnings
        )

        niche_log: list[str] = []
        niche_context = _context(settings, log=niche_log, expander=FakeExpander(niche_log, []))
        niche_data = await NewNicheResearch("seed").run(niche_context)
        assert niche_data.keywords == []
        assert any("no usable keywords" in w for w in niche_context.warnings), (
            niche_context.warnings
        )

        site_log: list[str] = []
        site_context = _context(settings, log=site_log, gsc=FakeGsc(site_log, rows=[]))
        site_data = await ExistingSiteResearch("https://example.com/").run(site_context)
        assert site_data.keywords == []
        assert any("no rows" in warning for warning in site_context.warnings), site_context.warnings

    anyio.run(exercise)


def test_a_run_out_of_time_does_not_start_the_clock_again(settings: Settings) -> None:
    """The expander keeps its own clock and starts it fresh on every call.

    `can_spend` refuses on a spent runtime as readily as on a spent count, so a
    competitor run past its ceiling took the same branch as one that had merely
    used its Ads allowance — and launched a fallback expansion entitled to the
    whole runtime budget a second time. The two also call for different actions,
    so they must not share a warning.
    """

    async def exercise() -> None:
        log: list[str] = []
        expander = FakeExpander(log, _candidates(3))
        context = _context(
            settings,
            log=log,
            ads=FakeAds(log),
            expander=expander,
            budget=Budget(max_runtime_seconds=0.001, max_keywords=10, max_trends_calls=1),
        )
        await anyio.sleep(0.01)
        data = await CompetitorResearch("example.com", seed_keyword="seed").run(context)

        assert "autocomplete" not in log, "the fallback restarted a clock that had run out"
        assert data.keywords == []
        assert any("ran out of time" in warning for warning in context.warnings), context.warnings
        assert not any("budget was spent" in warning for warning in context.warnings)

    anyio.run(exercise)


def test_a_fallback_that_found_nothing_says_so_too(settings: Settings) -> None:
    """The competitor fallback is a fan-out like any other and reports like one.

    Autocomplete ran because Google Ads was gone, and it came back with nothing
    usable. Silent, the empty run is explained by the missing Ads — which is
    true about Ads and wrong about why there are no keywords.
    """

    async def exercise() -> None:
        log: list[str] = []
        context = _context(settings, log=log, expander=FakeExpander(log, []))
        data = await CompetitorResearch("example.com", seed_keyword="seed").run(context)

        assert "autocomplete" in log, "the fallback has to have run for this to mean anything"
        assert data.keywords == []
        assert any("no usable keywords" in w for w in context.warnings), context.warnings

    anyio.run(exercise)


def test_a_failing_fan_out_is_not_a_fan_out_that_found_nothing(settings: Settings) -> None:
    """Warnings have to arrive in the order the events did.

    An empty envelope names its first warning as the cause, on the understanding
    that warnings accumulate as the run proceeds. The failure count used to be
    appended during the final assembly — after every later source had already
    warned — so a fan-out where two requests in three died reported that it had
    simply found nothing, which asserts the opposite of what happened.
    """

    async def exercise() -> None:
        log: list[str] = []
        expander = FakeExpander(log, [], queries_failed=2)
        context = _context(settings, log=log, expander=expander)
        data = await NewNicheResearch("seed").run(context)

        assert data.keywords == []
        assert "failed and were skipped" in context.warnings[0], context.warnings
        assert any("no usable keywords" in w for w in context.warnings)

    anyio.run(exercise)
