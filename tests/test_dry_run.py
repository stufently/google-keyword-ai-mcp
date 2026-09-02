from pathlib import Path
from typing import cast

import pytest

from google_keyword_ai.config import Settings
from google_keyword_ai.market import Market
from google_keyword_ai.pipeline.budget import Budget, BudgetGuard
from google_keyword_ai.pipeline.models import DryRunPlan
from google_keyword_ai.pipeline.scenarios import (
    AdsLike,
    CompetitorResearch,
    ExistingSiteResearch,
    ExpanderLike,
    NewNicheResearch,
    ScenarioContext,
    SearchConsoleLike,
    TrendsLike,
)
from google_keyword_ai.usecases import research as research_usecase


class CallBomb:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"plan() made an external call: {name}")


def _context(settings: Settings) -> ScenarioContext:
    bomb = CallBomb()
    return ScenarioContext(
        settings=settings,
        market=Market.parse("en", "US"),
        budget_guard=BudgetGuard(Budget(max_keywords=40, max_autocomplete_queries=10)),
        google_ads=cast(AdsLike, bomb),
        trends=cast(TrendsLike, bomb),
        search_console=cast(SearchConsoleLike, bomb),
        expander=cast(ExpanderLike, bomb),
        availability={
            "autocomplete": True,
            "google_ads": True,
            "trends": True,
            "search_console": True,
        },
    )


def test_plan_for_every_scenario_has_estimates_and_makes_no_calls(settings: Settings) -> None:
    context = _context(settings)
    plans = [
        NewNicheResearch("seed").plan(context),
        CompetitorResearch("example.com").plan(context),
        ExistingSiteResearch("https://example.com/").plan(context),
    ]

    assert [plan.scenario for plan in plans] == ["niche", "competitor", "site"]
    for plan in plans:
        assert plan.steps
        assert (
            plan.estimated_autocomplete_queries
            + plan.estimated_ads_calls
            + plan.estimated_trends_calls
            > 0
        )


def test_run_research_with_dry_run_never_builds_a_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Testing plan() alone leaves the flag itself unguarded.

    ``plan()`` is pure by construction, so calling it directly proves nothing
    about ``--dry-run``: if the use-case ignored the flag it would run the real
    research and every existing test would stay green. Make provider
    construction itself an error and go through the public entry point.
    """

    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("dry run built a provider")

    for name in (
        "AutocompleteProvider",
        "GoogleTrendsProvider",
        "GoogleAdsProvider",
        "SearchConsoleProvider",
    ):
        if hasattr(research_usecase, name):
            monkeypatch.setattr(research_usecase, name, explode)

    envelope = research_usecase.run_research(
        Settings(data_dir=tmp_path / "data"),
        "running shoes",
        dry_run=True,
    )

    plan = cast(DryRunPlan, envelope.data)
    assert plan.steps
    assert plan.estimated_autocomplete_queries > 0
