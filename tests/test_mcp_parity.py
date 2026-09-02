import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import anyio
import httpx
import respx
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from typer.testing import CliRunner

from google_keyword_ai.cli import main as cli_main
from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Envelope
from google_keyword_ai.mcp import server as mcp_server
from google_keyword_ai.mcp.server import build_server
from google_keyword_ai.normalize import KeywordCandidate
from google_keyword_ai.opportunities import Opportunity
from google_keyword_ai.providers.autocomplete import PRIMARY_ENDPOINT
from google_keyword_ai.providers.base import ProviderInfo
from google_keyword_ai.providers.expander import ExpansionLimits, ExpansionStats
from google_keyword_ai.providers.google_ads import KeywordIdea, KeywordMetrics
from google_keyword_ai.providers.trends.models import TrendPoint, TrendsResult
from google_keyword_ai.usecases.ads import AdsData
from google_keyword_ai.usecases.doctor import run_doctor
from google_keyword_ai.usecases.expand import ExpandData
from google_keyword_ai.usecases.gsc import OpportunitiesData
from google_keyword_ai.usecases.trends import TrendsData


def test_cli_and_mcp_doctor_have_identical_wire_envelopes(
    thread_offload: None, tmp_path: Path
) -> None:
    data_dir = tmp_path / "shared-data"
    server = build_server(Settings(data_dir=data_dir))

    async def call_doctor() -> dict[str, object]:
        async with (
            create_client_server_memory_streams() as (
                (client_read, client_write),
                (server_read, server_write),
            ),
            anyio.create_task_group() as task_group,
        ):
            low_level_server = server._lowlevel_server

            async def run_server() -> None:
                await low_level_server.run(
                    server_read,
                    server_write,
                    low_level_server.create_initialization_options(),
                    raise_exceptions=True,
                )

            task_group.start_soon(run_server)
            async with ClientSession(client_read, client_write) as client:
                await client.initialize()
                result = await client.call_tool("doctor", {})
            task_group.cancel_scope.cancel()

        assert result.is_error is not True
        assert result.structured_content is not None
        return cast(dict[str, object], result.structured_content)

    mcp_payload = anyio.run(call_doctor)
    environment = os.environ.copy()
    environment["GKAI_DATA_DIR"] = str(data_dir)
    cli_result = subprocess.run(
        [sys.executable, "-m", "google_keyword_ai.cli.main", "doctor", "--format", "json"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert cli_result.returncode == 0, cli_result.stderr
    assert mcp_payload == json.loads(cli_result.stdout)


def test_doctor_tool_is_synchronous_so_the_sdk_offloads_it(thread_offload: None) -> None:
    """Blocking work must not run on the event loop.

    The SDK routes sync tool functions through ``anyio.to_thread.run_sync`` and
    awaits async ones directly on the loop. ``run_doctor`` opens SQLite and later
    milestones add network and gRPC calls, so the tool has to stay synchronous.
    """
    server = build_server(Settings())
    tool = server._tool_manager.get_tool("doctor")
    assert tool is not None
    assert tool.is_async is False


def test_cli_and_mcp_suggest_have_identical_wire_envelopes(
    thread_offload: None, tmp_path: Path, monkeypatch: object
) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    settings = Settings(data_dir=tmp_path / "suggest-shared", http_max_attempts=1)
    server = build_server(settings)

    async def call_suggest() -> dict[str, object]:
        async with (
            create_client_server_memory_streams() as (
                (client_read, client_write),
                (server_read, server_write),
            ),
            anyio.create_task_group() as task_group,
        ):
            low_level_server = server._lowlevel_server

            async def run_server() -> None:
                await low_level_server.run(
                    server_read,
                    server_write,
                    low_level_server.create_initialization_options(),
                    raise_exceptions=True,
                )

            task_group.start_soon(run_server)
            async with ClientSession(client_read, client_write) as client:
                await client.initialize()
                result = await client.call_tool(
                    "suggest_keywords",
                    {"query": "seed", "language": "en", "country": "US", "limit": 1},
                )
            task_group.cancel_scope.cancel()

        assert result.is_error is not True
        assert result.structured_content is not None
        return cast(dict[str, object], result.structured_content)

    with respx.mock(assert_all_called=True) as router:
        route = router.get(
            PRIMARY_ENDPOINT,
            params={
                "client": "chrome",
                "ie": "utf-8",
                "oe": "utf-8",
                "q": "seed",
                "hl": "en",
                "gl": "US",
            },
        ).mock(return_value=httpx.Response(200, json=["seed", ["seed one"]]))
        mcp_payload = anyio.run(call_suggest)

    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    cli_result = CliRunner().invoke(
        cli_main.app,
        ["suggest", "seed", "--language", "en", "--country", "US", "--limit", "1"],
    )

    assert route.call_count == 1
    assert cli_result.exit_code == 0, cli_result.output
    assert mcp_payload == json.loads(cli_result.stdout)


def test_suggest_tool_is_synchronous(thread_offload: None) -> None:
    server = build_server(Settings())
    tool = server._tool_manager.get_tool("suggest_keywords")
    assert tool is not None
    assert tool.is_async is False


def _expand_envelope(seed: str) -> Envelope[ExpandData]:
    return Envelope(
        data=ExpandData(
            seed=seed,
            language="en",
            country="US",
            provider=ProviderInfo(name="autocomplete", official=False, stability="unofficial"),
            strategies=["digits"],
            limits=ExpansionLimits(max_depth=1, max_queries=2),
            stats=ExpansionStats(queries_executed=2, depth_reached=0),
            keywords=[
                KeywordCandidate(
                    raw="seed one",
                    normalized="seed one",
                    discovered_from=["autocomplete:digits:seed 1"],
                )
            ],
        )
    )


def test_cli_and_mcp_expand_have_identical_wire_envelopes(
    thread_offload: None,
    monkeypatch: object,
) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    settings = Settings()

    def fake_run_expand(_settings: Settings, seed: str, **_kwargs: object) -> Envelope[ExpandData]:
        return _expand_envelope(seed)

    monkeypatch.setattr(mcp_server, "run_expand", fake_run_expand)
    monkeypatch.setattr(cli_main, "run_expand", fake_run_expand)
    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    server = build_server(settings)

    async def call_expand() -> dict[str, object]:
        async with (
            create_client_server_memory_streams() as (
                (client_read, client_write),
                (server_read, server_write),
            ),
            anyio.create_task_group() as task_group,
        ):
            low_level_server = server._lowlevel_server

            async def run_server() -> None:
                await low_level_server.run(
                    server_read,
                    server_write,
                    low_level_server.create_initialization_options(),
                    raise_exceptions=True,
                )

            task_group.start_soon(run_server)
            async with ClientSession(client_read, client_write) as client:
                await client.initialize()
                result = await client.call_tool(
                    "expand_keywords",
                    {
                        "seed": "seed",
                        "language": "en",
                        "country": "US",
                        "strategies": ["digits"],
                        "limit": 1,
                    },
                )
            task_group.cancel_scope.cancel()

        assert result.is_error is not True
        assert result.structured_content is not None
        return cast(dict[str, object], result.structured_content)

    mcp_payload = anyio.run(call_expand)
    cli_result = CliRunner().invoke(
        cli_main.app,
        [
            "expand",
            "seed",
            "--language",
            "en",
            "--country",
            "US",
            "--strategy",
            "digits",
            "--limit",
            "1",
        ],
    )

    assert cli_result.exit_code == 0, cli_result.output
    assert mcp_payload == json.loads(cli_result.stdout)


def test_expand_tool_is_synchronous(thread_offload: None) -> None:
    server = build_server(Settings())
    tool = server._tool_manager.get_tool("expand_keywords")
    assert tool is not None
    assert tool.is_async is False


def test_analyze_trends_tool_has_usecase_parity(
    thread_offload: None,
    monkeypatch: object,
) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    settings = Settings()
    expected = Envelope(
        data=TrendsData(
            provider=ProviderInfo(name="trends", official=False, stability="unofficial"),
            result=TrendsResult(
                keywords=["one", "two"],
                geo="US",
                timeframe="today 12-m",
                normalization_scope="0123456789abcdef",
                timeline=[
                    TrendPoint(
                        timestamp=datetime(2026, 9, 1, tzinfo=UTC),
                        formatted_time="Sep 1, 2026",
                        values=[50, 100],
                        has_data=[True, True],
                    )
                ],
                retrieved_at=datetime(2026, 9, 2, tzinfo=UTC),
                source="https://trends.google.com/trends/api/explore",
            ),
        )
    )

    def fake_run_compare(
        _settings: Settings,
        keywords: list[str],
        **_kwargs: object,
    ) -> Envelope[TrendsData]:
        assert keywords == ["one", "two"]
        return expected

    monkeypatch.setattr(mcp_server, "run_trends_compare", fake_run_compare)
    server = build_server(settings)

    async def call_trends() -> dict[str, object]:
        async with (
            create_client_server_memory_streams() as (
                (client_read, client_write),
                (server_read, server_write),
            ),
            anyio.create_task_group() as task_group,
        ):
            low_level_server = server._lowlevel_server

            async def run_server() -> None:
                await low_level_server.run(
                    server_read,
                    server_write,
                    low_level_server.create_initialization_options(),
                    raise_exceptions=True,
                )

            task_group.start_soon(run_server)
            async with ClientSession(client_read, client_write) as client:
                await client.initialize()
                result = await client.call_tool(
                    "analyze_trends",
                    {"keywords": ["one", "two"], "language": "en", "country": "US"},
                )
            task_group.cancel_scope.cancel()

        assert result.is_error is not True
        assert result.structured_content is not None
        return cast(dict[str, object], result.structured_content)

    assert anyio.run(call_trends) == expected.to_wire()


def test_analyze_trends_tool_is_synchronous(thread_offload: None) -> None:
    server = build_server(Settings())
    tool = server._tool_manager.get_tool("analyze_trends")
    assert tool is not None
    assert tool.is_async is False


def test_get_keyword_metrics_has_usecase_parity(
    thread_offload: None,
    monkeypatch: object,
) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    settings = Settings()
    expected = Envelope(
        data=AdsData(
            provider=ProviderInfo(name="google_ads", official=True, stability="stable"),
            mode="historical_metrics",
            language="en",
            country="US",
            ideas=[
                KeywordIdea(
                    text="keyword",
                    metrics=KeywordMetrics(
                        avg_monthly_searches=100,
                        competition="LOW",
                    ),
                )
            ],
        )
    )

    def fake_run_historical(
        _settings: Settings,
        keywords: list[str],
        **_kwargs: object,
    ) -> Envelope[AdsData]:
        assert keywords == ["keyword"]
        return expected

    monkeypatch.setattr(mcp_server, "run_ads_historical", fake_run_historical)
    server = build_server(settings)

    async def call_metrics() -> dict[str, object]:
        async with (
            create_client_server_memory_streams() as (
                (client_read, client_write),
                (server_read, server_write),
            ),
            anyio.create_task_group() as task_group,
        ):
            low_level_server = server._lowlevel_server

            async def run_server() -> None:
                await low_level_server.run(
                    server_read,
                    server_write,
                    low_level_server.create_initialization_options(),
                    raise_exceptions=True,
                )

            task_group.start_soon(run_server)
            async with ClientSession(client_read, client_write) as client:
                await client.initialize()
                result = await client.call_tool(
                    "get_keyword_metrics",
                    {"keywords": ["keyword"], "language": "en", "country": "US"},
                )
            task_group.cancel_scope.cancel()

        assert result.is_error is not True
        assert result.structured_content is not None
        return cast(dict[str, object], result.structured_content)

    assert anyio.run(call_metrics) == expected.to_wire()


def test_google_ads_tools_are_synchronous() -> None:
    server = build_server(Settings())

    metrics = server._tool_manager.get_tool("get_keyword_metrics")
    competitor = server._tool_manager.get_tool("analyze_competitor")

    assert metrics is not None
    assert metrics.is_async is False
    assert competitor is not None
    assert competitor.is_async is False


def test_doctor_reports_google_ads_missing_credentials(tmp_path: Path) -> None:
    envelope = run_doctor(Settings(data_dir=tmp_path / "doctor"))

    google_ads = next(
        provider for provider in envelope.data.providers if provider.name == "google_ads"
    )
    assert google_ads.available is False
    assert google_ads.detail == "missing credentials"

    search_console = next(
        provider for provider in envelope.data.providers if provider.name == "search_console"
    )
    assert search_console.available is False
    assert search_console.detail == "missing credentials"


def test_find_gsc_opportunities_has_usecase_parity(
    thread_offload: None,
    monkeypatch: object,
) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    settings = Settings()
    expected = Envelope(
        data=OpportunitiesData(
            provider=ProviderInfo(name="search_console", official=True, stability="stable"),
            site_url="sc-domain:example.com",
            start_date="2026-08-01",
            end_date="2026-08-28",
            thresholds={"min_impressions": 100.0},
            opportunities=[
                Opportunity(
                    query="keyword",
                    page="https://example.com/page",
                    clicks=1,
                    impressions=100,
                    ctr=0.01,
                    position=8,
                    kind="quick_win",
                    reason="test",
                )
            ],
            truncated=False,
        )
    )

    def fake_run(
        _settings: Settings,
        site_url: str,
        **kwargs: object,
    ) -> Envelope[OpportunitiesData]:
        assert site_url == "sc-domain:example.com"
        assert kwargs == {"days": 14, "country": "US", "limit": 3}
        return expected

    monkeypatch.setattr(mcp_server, "run_gsc_opportunities", fake_run)
    server = build_server(settings)

    async def call_tool() -> dict[str, object]:
        async with (
            create_client_server_memory_streams() as (
                (client_read, client_write),
                (server_read, server_write),
            ),
            anyio.create_task_group() as task_group,
        ):
            low_level_server = server._lowlevel_server

            async def run_server() -> None:
                await low_level_server.run(
                    server_read,
                    server_write,
                    low_level_server.create_initialization_options(),
                    raise_exceptions=True,
                )

            task_group.start_soon(run_server)
            async with ClientSession(client_read, client_write) as client:
                await client.initialize()
                result = await client.call_tool(
                    "find_gsc_opportunities",
                    {
                        "site_url": "sc-domain:example.com",
                        "days": 14,
                        "country": "US",
                        "limit": 3,
                    },
                )
            task_group.cancel_scope.cancel()

        assert result.is_error is not True
        assert result.structured_content is not None
        return cast(dict[str, object], result.structured_content)

    assert anyio.run(call_tool) == expected.to_wire()


def test_find_gsc_opportunities_tool_is_synchronous(thread_offload: None) -> None:
    server = build_server(Settings())
    tool = server._tool_manager.get_tool("find_gsc_opportunities")
    assert tool is not None
    assert tool.is_async is False
