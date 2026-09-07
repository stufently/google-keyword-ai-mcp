"""Research demand contracts: selection, budgets, statuses and consumer behavior."""

from collections.abc import Sequence
from datetime import UTC, datetime
from functools import partial

import anyio
import pytest

from google_keyword_ai import demand as demand_module
from google_keyword_ai.config import Settings
from google_keyword_ai.demand import DemandStatus
from google_keyword_ai.envelope import Completeness
from google_keyword_ai.errors import NetworkError
from google_keyword_ai.market import Market
from google_keyword_ai.normalize import KeywordCandidate
from google_keyword_ai.pipeline.budget import Budget, BudgetGuard
from google_keyword_ai.pipeline.models import ResearchData, ResearchKeyword
from google_keyword_ai.pipeline.scenarios import (
    CompetitorResearch,
    ExistingSiteResearch,
    NewNicheResearch,
    ScenarioContext,
)
from google_keyword_ai.providers.google_ads import KeywordIdea, KeywordMetrics
from google_keyword_ai.providers.search_console import SearchAnalyticsRow
from google_keyword_ai.providers.trends.models import TrendPoint, TrendsResult
from google_keyword_ai.reports.markdown import render_markdown
from google_keyword_ai.usecases.research import _envelope_for_research
from test_reports import research
from test_scenarios import FakeAds, FakeExpander, FakeGsc


class DemandTrends:
    def __init__(self, mode: str = "measured") -> None:
        self.calls: list[list[str]] = []
        self.warnings: list[str] = []
        self.mode = mode
        self.guard: BudgetGuard | None = None

    async def fetch(
        self, keywords: Sequence[str], *, geo: str, timeframe: str, hl: str
    ) -> TrendsResult:
        self.calls.append(list(keywords))
        self.warnings = []
        comparison = len(keywords) > 1
        if comparison and self.mode == "batch_failed":
            raise NetworkError("comparison offline")
        if comparison and self.mode == "timeline_failed":
            self.warnings = ["TIMESERIES: offline"]
        if comparison and self.mode == "ancillary_failed":
            self.warnings = ["RELATED_QUERIES: offline", "GEO_MAP: offline"]
        if comparison and self.mode == "runtime" and self.guard is not None:
            self.guard._started_at = anyio.current_time() - 301
        return TrendsResult(
            keywords=list(keywords),
            geo=geo,
            timeframe=timeframe,
            normalization_scope=hl,
            retrieved_at=datetime(2026, 9, 7, tzinfo=UTC),
            source="offline test",
            timeline=[]
            if self.mode == "timeline_failed" and comparison
            else [
                TrendPoint(
                    timestamp=datetime(2026, 1, week + 1, tzinfo=UTC),
                    formatted_time="week",
                    values=[
                        0
                        if comparison and self.mode == "anchor_collapsed" and index == 0
                        else 40
                        if index == 0
                        else 0
                        if self.mode == "zero"
                        else 20
                        for index in range(len(keywords))
                    ],
                    has_data=[
                        not comparison
                        or index == 0
                        or (
                            self.mode != "below_resolution"
                            and (self.mode != "low_coverage" or week == 0)
                        )
                        for index in range(len(keywords))
                    ],
                )
                for week in range(8)
            ],
        )


def context_for(
    settings: Settings,
    *,
    count: int = 12,
    calls: int = 3,
    mode: str = "measured",
    demand: bool = True,
    anchor: str | None = None,
) -> tuple[ScenarioContext, DemandTrends]:
    names = ["seed", *[f"candidate {i:02}" for i in range(count)]]
    log: list[str] = []
    trends = DemandTrends(mode)
    context = ScenarioContext(
        settings=settings,
        market=Market.parse("en", "US"),
        budget_guard=BudgetGuard(Budget(max_trends_calls=calls)),
        expander=FakeExpander(
            log,
            [
                KeywordCandidate(
                    raw=name, normalized=name, discovered_from=["autocomplete"], relevance=1000 - i
                )
                for i, name in enumerate(names)
            ],
        ),
        google_ads=FakeAds(
            log,
            ideas=[
                KeywordIdea(text=name, metrics=KeywordMetrics(avg_monthly_searches=1000 - i))
                for i, name in enumerate(names)
            ],
        ),
        search_console=FakeGsc(
            log,
            rows=[
                SearchAnalyticsRow(
                    keys={"query": name, "page": "/"},
                    clicks=5,
                    impressions=1000 - i,
                    ctr=0.01,
                    position=8,
                )
                for i, name in enumerate(names)
            ],
        ),
        trends=trends,
        demand=demand,
        demand_anchor=anchor,
    )
    trends.guard = context.budget_guard
    return context, trends


async def niche(
    settings: Settings,
    *,
    count: int = 12,
    calls: int = 3,
    mode: str = "measured",
    demand: bool = True,
    anchor: str | None = None,
) -> tuple[ResearchData, ScenarioContext, DemandTrends]:
    context, trends = context_for(
        settings, count=count, calls=calls, mode=mode, demand=demand, anchor=anchor
    )
    return await NewNicheResearch("seed").run(context), context, trends


def test_selector_excludes_normalized_seed() -> None:
    anchor, participants = demand_module.select_research_candidates(
        ["  SEED ", "first", "second", "third"], "Seed", 2
    )
    assert "seed" not in [anchor, *participants]
    assert (anchor, participants) == ("first", ["second", "third"])


def test_selector_uses_top_candidate_as_anchor() -> None:
    anchor, participants = demand_module.select_research_candidates(
        ["top", "middle", "last"], "seed", 1
    )
    assert anchor == "top"
    assert participants == ["middle", "last"]


def test_selector_reserves_room_for_anchor() -> None:
    anchor, participants = demand_module.select_research_candidates(
        [f"candidate {i:02}" for i in range(12)], "seed", 2
    )
    assert len([anchor, *participants]) == 9
    assert participants[-1] == "candidate 08"


@pytest.mark.parametrize("scenario", ["niche", "competitor", "site"])
@pytest.mark.parametrize("enabled", [False, True])
def test_all_scenarios_share_optional_demand(
    settings: Settings, scenario: str, enabled: bool
) -> None:
    async def exercise() -> None:
        context, trends = context_for(settings, demand=enabled)
        scenarios: dict[str, NewNicheResearch | CompetitorResearch | ExistingSiteResearch] = {
            "niche": NewNicheResearch("seed"),
            "competitor": CompetitorResearch("example.com"),
            "site": ExistingSiteResearch("https://example.com/"),
        }
        data = await scenarios[scenario].run(context)
        assert trends.calls[0] == ["seed"]
        assert len(trends.calls) == (3 if enabled else 1)
        if enabled:
            assert data.stats.demand is not None
            assert data.stats.demand.anchor == "candidate 00"
            assert data.stats.demand.ranked == 9
            assert all("seed" not in batch for batch in trends.calls[1:])
        else:
            assert data.stats.demand is None
            assert data.keywords
            assert all(row.demand_status is None for row in data.keywords)
            assert all(
                value is None
                for row in data.keywords
                for key, value in row.model_dump().items()
                if key.startswith("demand_")
            )
            assert "Relative demand" not in render_markdown(data, [], [])
            envelope = _envelope_for_research(data, context.warnings, context.errors)
            assert envelope.completeness is Completeness.COMPLETE

    anyio.run(exercise)


def test_demand_batches_are_recorded_in_spend(settings: Settings) -> None:
    data, _, trends = anyio.run(partial(niche, settings, count=9))
    assert data.stats.spend.trends_calls == 3
    assert data.stats.demand is not None
    assert data.stats.demand.batches == 2
    assert trends.calls[1:] == [
        ["candidate 00", "candidate 01", "candidate 02", "candidate 03", "candidate 04"],
        ["candidate 00", "candidate 05", "candidate 06", "candidate 07", "candidate 08"],
    ]
    assert data.keywords[1].demand_relative == 50.0
    assert data.keywords[0].demand_relative == 100.0


def test_budget_truncation_is_partial_and_named(settings: Settings) -> None:
    data, context, _ = anyio.run(partial(niche, settings))
    unrelated_warning = "Optional source is unavailable."
    context.warnings.insert(0, unrelated_warning)
    envelope = _envelope_for_research(data, context.warnings, context.errors)
    assert unrelated_warning in envelope.warnings
    assert data.stats.demand is not None
    assert data.stats.demand.truncated_by_budget is True
    assert data.stats.demand.requested == 12
    assert data.stats.demand.ranked == 9
    assert envelope.completeness is Completeness.PARTIAL
    assert "max_trends_calls" in (envelope.completeness_reason or "")


def test_zero_remaining_batches_are_reported(settings: Settings) -> None:
    data, context, trends = anyio.run(partial(niche, settings, calls=1))
    assert trends.calls == [["seed"]]
    assert data.stats.demand is not None
    assert data.stats.demand.ranked == data.stats.demand.batches == 0
    assert data.stats.demand.anchor is None
    assert all(row.demand_status is None for row in data.keywords)
    envelope = _envelope_for_research(data, context.warnings, context.errors)
    assert envelope.completeness is Completeness.PARTIAL
    assert "max_trends_calls" in (envelope.completeness_reason or "")


def test_unranked_none_is_separate_from_all_five_statuses(settings: Settings) -> None:
    data, _, _ = anyio.run(partial(niche, settings))
    assert data.keywords[9].demand_status is None
    assert data.keywords[9].demand_status not in list(DemandStatus)
    assert all(row.demand_status is DemandStatus.MEASURED for row in data.keywords[:9])
    assert "not ranked" in render_markdown(data, [], [])


@pytest.mark.parametrize(
    "mode",
    [
        "measured",
        "zero",
        "low_coverage",
        "below_resolution",
        "anchor_collapsed",
        "batch_failed",
        "timeline_failed",
    ],
)
def test_demand_statuses_and_envelope(settings: Settings, mode: str) -> None:
    data, context, _ = anyio.run(partial(niche, settings, count=5, mode=mode))
    row = data.keywords[1]
    expected = {"zero": "measured", "timeline_failed": "batch_failed"}.get(mode, mode)
    assert row.demand_status == expected
    envelope = _envelope_for_research(data, context.warnings, context.errors)
    if expected == "measured":
        assert envelope.completeness is Completeness.COMPLETE
        assert row.demand_relative == (0.0 if mode == "zero" else 50.0)
    else:
        assert row.demand_relative is None
        assert row.demand_reason
        assert envelope.completeness is Completeness.PARTIAL
    rendered = render_markdown(data, [], [])
    assert expected in rendered
    assert "Relative demand" in rendered


def test_runtime_stops_before_next_batch(settings: Settings) -> None:
    data, context, trends = anyio.run(partial(niche, settings, mode="runtime"))
    assert len(trends.calls) == 2
    assert data.stats.spend.trends_calls == 2
    assert data.stats.demand is not None
    assert data.stats.demand.ranked == 5
    assert data.stats.demand.truncated_by_budget is True
    assert data.keywords[5].demand_status is None
    envelope = _envelope_for_research(data, context.warnings, context.errors)
    assert "max_runtime_seconds" in (envelope.completeness_reason or "")


def test_explicit_anchor_outside_initial_budget_is_included(settings: Settings) -> None:
    data, _, trends = anyio.run(partial(niche, settings, anchor=" CANDIDATE 11 "))
    assert data.stats.demand is not None
    assert data.stats.demand.anchor == "candidate 11"
    assert trends.calls[1][0] == "candidate 11"
    assert data.keywords[11].demand_relative == 100.0
    assert data.keywords[8].demand_status is None


def test_renderer_refuses_unknown_demand_status() -> None:
    row = ResearchKeyword(keyword="future", normalized="future", discovered_from=[])
    row = row.model_copy(update={"demand_status": "future_status", "demand_relative": None})
    reason = None
    try:
        render_markdown(research(keywords=[row]), [], [])
    except ValueError as exc:
        reason = str(exc)
    assert reason is not None
    assert "future_status" in reason


@pytest.mark.parametrize("count", [0, 1])
def test_too_few_candidates_skip_demand_with_explanation(settings: Settings, count: int) -> None:
    data, context, trends = anyio.run(partial(niche, settings, count=count))
    assert len(trends.calls) == 1
    assert data.stats.demand is not None
    assert data.stats.demand.requested == count
    assert data.stats.demand.ranked == 0
    assert data.stats.demand.truncated_by_budget is False
    assert any("at least two" in warning for warning in context.warnings)


def test_coverage_setting_is_used_in_research(settings: Settings) -> None:
    settings = settings.model_copy(update={"demand_min_coverage": 0.0})
    data, context, _ = anyio.run(partial(niche, settings, count=5, mode="low_coverage"))
    assert data.keywords[1].demand_status is DemandStatus.MEASURED
    assert data.keywords[1].demand_relative == 50.0
    assert data.keywords[1].demand_measured_weeks == 1
    assert data.keywords[1].demand_weeks == 8
    assert _envelope_for_research(data, context.warnings, context.errors).completeness is (
        Completeness.COMPLETE
    )


def test_unavailable_trends_leaves_unranked_rows_and_explains(settings: Settings) -> None:
    async def exercise() -> None:
        context, trends = context_for(settings, count=5)
        context.trends = None
        data = await NewNicheResearch("seed").run(context)
        assert not trends.calls
        assert data.stats.spend.trends_calls == 0
        assert data.stats.demand is not None
        assert data.stats.demand.ranked == 0
        assert data.stats.demand.requested == 5
        assert all(row.demand_status is None for row in data.keywords)
        assert any("Trends is unavailable" in warning for warning in context.warnings)

    anyio.run(exercise)


def test_runtime_exhausted_before_demand_ranks_nothing(settings: Settings) -> None:
    async def exercise() -> None:
        context, trends = context_for(settings)
        context.budget_guard._started_at = anyio.current_time() - 301
        data = await NewNicheResearch("seed").run(context)
        assert not trends.calls
        assert data.stats.demand is not None
        assert data.stats.demand.ranked == data.stats.demand.batches == 0
        assert data.stats.demand.truncated_by_budget is True
        assert data.stats.stopped_by == "max_runtime_seconds"

    anyio.run(exercise)


def test_ancillary_widget_failures_do_not_downgrade_demand(settings: Settings) -> None:
    data, context, _ = anyio.run(partial(niche, settings, count=5, mode="ancillary_failed"))
    envelope = _envelope_for_research(
        data, context.warnings, context.errors, notices=context.notices
    )
    assert envelope.completeness is Completeness.COMPLETE
    assert "RELATED_QUERIES: offline" in data.data_quality.caveats
    assert all(row.demand_relative is not None for row in data.keywords)
