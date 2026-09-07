"""Offline HTTP and SDK boundary tests for the optional research demand column."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import anyio
import httpx
import pytest
import respx
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from typer.testing import CliRunner

from google_keyword_ai.cli import main as cli_main
from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness
from google_keyword_ai.market import Market
from google_keyword_ai.mcp.server import build_server
from google_keyword_ai.pipeline.budget import Budget, BudgetGuard
from google_keyword_ai.pipeline.models import DryRunPlan, ResearchData
from google_keyword_ai.pipeline.scenarios import ScenarioContext
from google_keyword_ai.providers.autocomplete import PRIMARY_ENDPOINT
from google_keyword_ai.storage.engine import open_database
from google_keyword_ai.usecases import research as research_module
from google_keyword_ai.usecases import runs as runs_module
from google_keyword_ai.usecases.research import run_research
from google_keyword_ai.usecases.runs import run_rerun, run_resume, run_show
from test_demand_usecase import mock_trends, settings_for
from test_research_demand import context_for


async def call_mcp(
    settings: Settings, name: str, arguments: dict[str, object]
) -> dict[str, object]:
    server = build_server(settings)
    async with (
        create_client_server_memory_streams() as (
            (client_read, client_write),
            (server_read, server_write),
        ),
        anyio.create_task_group() as group,
    ):
        low_level = server._lowlevel_server

        async def serve() -> None:
            await low_level.run(
                server_read,
                server_write,
                low_level.create_initialization_options(),
                raise_exceptions=True,
            )

        group.start_soon(serve)
        async with ClientSession(client_read, client_write) as client:
            await client.initialize()
            result = await client.call_tool(name, arguments)
        group.cancel_scope.cancel()
    assert not result.is_error
    assert result.structured_content is not None
    return cast(dict[str, object], result.structured_content)


def mock_research_http(router: respx.MockRouter) -> list[list[str]]:
    def suggest(request: httpx.Request) -> httpx.Response:
        seed = request.url.params["q"]
        return httpx.Response(200, json=[seed, [f"candidate {i:02}" for i in range(12)]])

    router.get(PRIMARY_ENDPOINT).mock(side_effect=suggest)
    return mock_trends(router)


@pytest.mark.parametrize("anchor", ["outside", " SEED "])
def test_explicit_anchor_outside_candidates_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    anchor: str,
) -> None:
    settings = settings_for(tmp_path)
    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    with respx.mock(assert_all_called=True) as router:
        calls = mock_research_http(router)
        result = CliRunner().invoke(
            cli_main.app,
            [
                "research",
                "seed",
                "--max-autocomplete-queries",
                "1",
                "--demand",
                "--demand-anchor",
                anchor,
            ],
        )
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["completeness"] == "empty"
        assert "anchor" in payload["completeness_reason"]
        assert calls == [["seed"]]


def test_demand_cli_and_mcp_wire_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_for(tmp_path)
    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    with respx.mock(assert_all_called=True) as router:
        calls = mock_research_http(router)
        cli = CliRunner().invoke(
            cli_main.app,
            [
                "research",
                "seed",
                "--max-autocomplete-queries",
                "1",
                "--demand",
                "--demand-anchor",
                "candidate 11",
            ],
        )
        assert cli.exit_code == 1, cli.output
        mcp = anyio.run(
            call_mcp,
            settings,
            "research_keywords",
            {
                "target": "seed",
                "max_autocomplete_queries": 1,
                "demand": True,
                "demand_anchor": "candidate 11",
            },
        )
    wire = json.loads(cli.stdout)
    # Collection timestamps and elapsed time describe separate executions.
    for payload in (wire, mcp):
        data = cast(dict[str, object], payload["data"])
        quality = cast(dict[str, object], data["data_quality"])
        quality.pop("retrieved_at")
        stats = cast(dict[str, object], data["stats"])
        cast(dict[str, object], stats["spend"]).pop("elapsed_seconds")
    assert wire == mcp
    assert wire["data"]["stats"]["demand"] == {
        "anchor": "candidate 11",
        "ranked": 9,
        "requested": 12,
        "batches": 2,
        "truncated_by_budget": True,
    }
    assert "max_trends_calls" in wire["completeness_reason"]
    assert calls == [
        ["seed"],
        ["candidate 11", "candidate 00", "candidate 01", "candidate 02", "candidate 03"],
        ["candidate 11", "candidate 04", "candidate 05", "candidate 06", "candidate 07"],
    ]


def test_anchor_alone_enables_demand_and_dry_plan_includes_cost(settings: Settings) -> None:
    result = run_research(settings, "seed", demand_anchor="candidate 02", dry_run=True)
    assert isinstance(result.data, DryRunPlan)
    assert result.data.estimated_trends_calls == 3
    assert any("demand" in step.lower() for step in result.data.steps)


@pytest.mark.parametrize("mode", ["measured", "ancillary_failed"])
def test_saved_demand_survives_resume_replay_and_rerun(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    contexts: list[ScenarioContext] = []

    @asynccontextmanager
    async def live(
        active: Settings,
        market: Market,
        budget: Budget,
        cache: object,
    ) -> AsyncIterator[ScenarioContext]:
        context, _ = context_for(active, count=9, demand=False, mode=mode)
        context.market = market
        context.budget_guard = BudgetGuard(budget)
        contexts.append(context)
        yield context

    monkeypatch.setattr(research_module, "_live_context", live)
    monkeypatch.setattr(runs_module, "_live_context", live)
    unsaved = run_research(settings, "seed", demand=True)
    assert unsaved.completeness is Completeness.COMPLETE
    assert isinstance(unsaved.data, ResearchData)
    if mode == "ancillary_failed":
        assert "RELATED_QUERIES: offline" in unsaved.data.data_quality.caveats
    saved = run_research(settings, "seed", demand_anchor="candidate 08", save_run=True)
    assert saved.run_id is not None
    assert isinstance(saved.data, ResearchData)
    assert saved.data.stats.demand is not None
    assert saved.data.stats.demand.anchor == "candidate 08"
    record = run_show(settings, saved.run_id).data
    assert record is not None
    assert record.stages[-1].name == "demand"
    restored = run_resume(settings, saved.run_id)
    assert restored.completeness is Completeness.COMPLETE
    assert restored.data is not None
    assert restored.data.stats.demand == saved.data.stats.demand
    assert contexts[-1].budget_guard._spend.trends_calls == 0
    engine = open_database(settings)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE runs SET app_version = 'old' WHERE run_id = ?", (saved.run_id,)
            )
    finally:
        engine.dispose()
    replayed = run_resume(settings, saved.run_id)
    rerun = run_rerun(settings, saved.run_id)
    for result in (replayed, rerun):
        assert result.completeness is Completeness.COMPLETE
        assert result.data is not None
        assert result.data.stats.demand == saved.data.stats.demand
        assert result.data.stats.spend.trends_calls == 3
