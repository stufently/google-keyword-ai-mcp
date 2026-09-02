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
from google_keyword_ai.usecases import research as research_module
from google_keyword_ai.usecases.research import _select_scenario, run_research


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
