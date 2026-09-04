from datetime import UTC, datetime
from typing import cast

import anyio
import pytest

from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness
from google_keyword_ai.errors import InvalidConfigurationError
from google_keyword_ai.market import Market
from google_keyword_ai.pipeline.budget import Budget, BudgetGuard, BudgetSpend
from google_keyword_ai.pipeline.models import (
    DataQuality,
    ResearchData,
    ResearchKeyword,
    ResearchStats,
    SourceUsage,
)
from google_keyword_ai.pipeline.scenarios import (
    ADS_CAVEAT,
    SITE_SEED_CAVEAT,
    TRENDS_CAVEAT,
    CompetitorResearch,
    ExistingSiteResearch,
    NewNicheResearch,
    ScenarioContext,
    SearchConsoleLike,
    _quality,
)
from google_keyword_ai.providers.search_console import SiteProperty
from google_keyword_ai.providers.trends.models import (
    TrendsResult,
    build_normalization_scope,
)
from google_keyword_ai.usecases import research as research_module
from google_keyword_ai.usecases.research import (
    _envelope_for_research,
    _select_scenario,
    run_research,
)


class PropertyOnlyGsc:
    async def list_properties(self) -> list[SiteProperty]:
        return [SiteProperty(site_url="https://owned.example/", permission_level="siteOwner")]


def test_auto_scenario_selection_distinguishes_topic_domain_url_and_property(
    settings: Settings,
) -> None:
    async def exercise() -> None:
        context = ScenarioContext(
            settings=settings,
            market=Market.parse("en", "US"),
            budget_guard=BudgetGuard(Budget()),
            search_console=cast(SearchConsoleLike, PropertyOnlyGsc()),
        )
        topic = await _select_scenario(context, "auto", "running shoes", None)
        domain = await _select_scenario(context, "auto", "example.com", None)
        url = await _select_scenario(context, "auto", "https://example.com/path", None)
        prop = await _select_scenario(context, "auto", "https://owned.example/", None)
        assert isinstance(topic, NewNicheResearch)
        assert isinstance(domain, CompetitorResearch)
        assert isinstance(url, CompetitorResearch)
        assert isinstance(prop, ExistingSiteResearch)

    anyio.run(exercise)


def test_sc_domain_stays_site_research_when_the_property_is_unreachable(
    settings: Settings,
) -> None:
    """`sc-domain:` is a Search Console identifier, not a web address.

    Auto-selection used to fall through to competitor research whenever the
    property could not be confirmed — no Search Console configured, or the
    account does not own it. That contradicted the plan the dry run had just
    shown the user and sent `sc-domain:...` to Google Ads as a site URL.
    """

    async def exercise() -> None:
        without_gsc = ScenarioContext(
            settings=settings,
            market=Market.parse("en", "US"),
            budget_guard=BudgetGuard(Budget()),
        )
        unowned = ScenarioContext(
            settings=settings,
            market=Market.parse("en", "US"),
            budget_guard=BudgetGuard(Budget()),
            search_console=cast(SearchConsoleLike, PropertyOnlyGsc()),
        )
        for context in (without_gsc, unowned):
            selected = await _select_scenario(context, "auto", "sc-domain:example.com", None)
            assert isinstance(selected, ExistingSiteResearch)

    anyio.run(exercise)


def test_explicit_scenario_overrides_auto_and_unknown_is_rejected(settings: Settings) -> None:
    explicit = run_research(settings, "plain topic", scenario="site", dry_run=True)
    assert explicit.data.scenario == "site"
    with pytest.raises(InvalidConfigurationError, match="Unknown research scenario"):
        run_research(settings, "topic", scenario="mystery", dry_run=True)


def _data(settings: Settings, *, with_keyword: bool) -> ResearchData:
    keywords = (
        [
            ResearchKeyword(
                keyword="one",
                normalized="one",
                discovered_from=["autocomplete"],
            )
        ]
        if with_keyword
        else []
    )
    return ResearchData(
        scenario="niche",
        input="topic",
        language="en",
        country="US",
        keywords=keywords,
        stats=ResearchStats(spend=BudgetSpend()),
        data_quality=DataQuality(
            sources=[SourceUsage(name="autocomplete", used=True, available=True, detail="used")],
            retrieved_at=datetime.now(UTC),
            absolute_metrics=[],
            relative_metrics=[],
            derived_metrics=[],
            caveats=[],
        ),
    )


def test_provider_failure_becomes_partial_and_fully_empty_is_empty(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def partial_execute(
        active_settings: Settings,
        target: str,
        scenario: str,
        market: Market,
        seed_keyword: str | None,
        budget: Budget,
        limit: int | None,
    ) -> tuple[ResearchData, list[str], list[str]]:
        del target, scenario, market, seed_keyword, budget, limit
        return _data(active_settings, with_keyword=True), [], ["provider failed"]

    monkeypatch.setattr(research_module, "_execute", partial_execute)
    partial = run_research(settings, "topic")
    assert partial.completeness is Completeness.PARTIAL
    assert partial.errors == ["provider failed"]

    async def empty_execute(
        active_settings: Settings,
        target: str,
        scenario: str,
        market: Market,
        seed_keyword: str | None,
        budget: Budget,
        limit: int | None,
    ) -> tuple[ResearchData, list[str], list[str]]:
        del target, scenario, market, seed_keyword, budget, limit
        return _data(active_settings, with_keyword=False), ["nothing available"], []

    monkeypatch.setattr(research_module, "_execute", empty_execute)
    empty = run_research(settings, "topic")
    assert empty.completeness is Completeness.EMPTY
    assert empty.completeness_reason == "nothing available"


def test_caveats_include_all_three_prohibitions_when_sources_participate(
    settings: Settings,
) -> None:
    context = ScenarioContext(
        settings=settings,
        market=Market.parse("en", "US"),
        budget_guard=BudgetGuard(Budget()),
        availability={
            "autocomplete": True,
            "google_ads": True,
            "trends": True,
            "search_console": False,
        },
    )
    quality = _quality(context, {"google_ads", "trends"}, site_seed=True)
    assert TRENDS_CAVEAT in quality.caveats
    assert ADS_CAVEAT in quality.caveats
    assert SITE_SEED_CAVEAT in quality.caveats


@pytest.mark.parametrize("budget_stop", ["max_runtime_seconds", "max_ads_calls"])
def test_an_empty_research_result_names_the_budget_that_stopped_it(
    settings: Settings, budget_stop: str
) -> None:
    """A run cut short before it gathered anything must not read as a verdict.

    "no research data" is a statement about the niche. A budget that ended the
    run before the first stage finished is a statement about the budget, and
    the caller who cannot tell them apart abandons a niche they never measured.
    """
    data = _data(settings, with_keyword=False)
    data.stats.stopped_by = budget_stop

    envelope = _envelope_for_research(data, [], [])

    assert envelope.completeness is Completeness.EMPTY
    assert envelope.completeness_reason == f"stopped by {budget_stop}"


def test_an_empty_research_result_with_nothing_to_blame_still_says_so(
    settings: Settings,
) -> None:
    """Without a stop, a warning or an error, the niche really was empty."""
    envelope = _envelope_for_research(_data(settings, with_keyword=False), [], [])

    assert envelope.completeness is Completeness.EMPTY
    assert envelope.completeness_reason == "no research data"


def test_a_reported_problem_outranks_the_budget_stop_as_the_reason(
    settings: Settings,
) -> None:
    """An error explains an empty answer better than the stop that followed it.

    A provider that refused is why there is nothing; the budget stop is only
    what happened next. The order matches the partial branch, so one rule
    covers both.
    """
    data = _data(settings, with_keyword=False)
    data.stats.stopped_by = "max_runtime_seconds"

    envelope = _envelope_for_research(data, ["a warning"], ["provider refused"])

    assert envelope.completeness_reason == "provider refused"


def test_the_budget_stop_outranks_a_warning_as_the_reason_for_nothing(
    settings: Settings,
) -> None:
    """A skipped optional source explains a thin answer, not an absent one.

    Research warnings are mostly "this source was skipped": true, reported in
    `warnings`, and no answer to why there are no keywords. The stop is. Read
    the other way round, a run that never measured the niche reports the
    absence of Ads as its verdict on it.
    """
    data = _data(settings, with_keyword=False)
    data.stats.stopped_by = "max_runtime_seconds"

    envelope = _envelope_for_research(data, ["Google Ads was skipped"], [])

    assert envelope.completeness_reason == "stopped by max_runtime_seconds"


def test_the_first_warning_is_the_reason_in_both_branches(settings: Settings) -> None:
    """One field, one rule for choosing it -- whether or not anything arrived.

    Warnings accumulate as the run proceeds, so the last one stands furthest
    from the cause. The partial branch already reports the first; the empty
    branch reported the last, which meant the same two warnings explained the
    same run differently depending on whether it returned a keyword.
    """
    empty = _envelope_for_research(
        _data(settings, with_keyword=False), ["first cause", "later effect"], []
    )
    partial = _envelope_for_research(
        _data(settings, with_keyword=True), ["first cause", "later effect"], []
    )

    assert empty.completeness is Completeness.EMPTY
    assert partial.completeness is Completeness.PARTIAL
    assert empty.completeness_reason == partial.completeness_reason == "first cause"


def test_a_trends_object_holding_nothing_is_not_data(settings: Settings) -> None:
    """A container is not its contents.

    Trends is fetched with the seed even when the run found no keywords, and a
    request whose widgets all failed still comes back as a `TrendsResult` --
    an empty one. Testing the object for existence rather than for rows made
    that outage count as data, and a run with nothing in it at all reported
    `partial`, which promises the caller usable data it does not have.
    """
    data = _data(settings, with_keyword=False)
    data.trends = TrendsResult(
        keywords=["topic"],
        geo="US",
        timeframe="today 12-m",
        normalization_scope=build_normalization_scope(
            ["topic"], geo="US", timeframe="today 12-m", hl="en"
        ),
        retrieved_at=datetime.now(UTC),
        source="https://trends.google.com/trends/api/explore",
    )

    envelope = _envelope_for_research(data, ["TIMESERIES: rate limited"], [])

    assert envelope.completeness is Completeness.EMPTY
    assert envelope.completeness_reason == "TIMESERIES: rate limited"
