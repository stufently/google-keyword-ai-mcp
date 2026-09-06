import json
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
from google_keyword_ai.mcp.server import build_server
from google_keyword_ai.providers.trends.unofficial import EXPLORE_URL, WARMUP_URL, WIDGETDATA_URL
from google_keyword_ai.usecases.demand import (
    RELATIVE_CAVEAT,
    RESOLUTION_CAVEAT,
    STITCHING_CAVEAT,
    run_demand,
)


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        http_max_attempts=1,
        trends_pacing_seconds=0.001,
        trends_circuit_breaker_failures=20,
    )


def mock_trends(
    router: respx.MockRouter,
    *,
    failures: dict[int, int | str] | None = None,
    unmeasured: bool = False,
    collapsed: bool = False,
    widget_noise: bool = False,
    timeline_failure: bool = False,
) -> list[list[str]]:
    """Mock HTTP only; exercise provider parsing, cache and orchestration together."""
    calls: list[list[str]] = []
    router.get(WARMUP_URL).respond(200)

    def explore(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.url.params["req"])
        keys = [item["keyword"] for item in payload["comparisonItem"]]
        calls.append(keys)
        failure = (failures or {}).get(len(calls))
        if failure == "network":
            raise httpx.ConnectError("offline", request=request)
        if failure == "api":
            return httpx.Response(200, text="invalid JSON")
        if isinstance(failure, int):
            return httpx.Response(failure, text="failed batch")
        widgets = [{"id": "TIMESERIES", "token": "test", "request": {"keys": keys}}]
        if widget_noise:
            widgets.extend(
                [
                    {"id": "GEO_MAP", "token": "test", "request": {}},
                    {"id": "RELATED_QUERIES_0", "token": "test", "request": {}},
                ]
            )
        return httpx.Response(200, json={"widgets": widgets})

    def timeline(request: httpx.Request) -> httpx.Response:
        if timeline_failure:
            return httpx.Response(500, text="timeline unavailable")
        keys = json.loads(request.url.params["req"])["keys"]
        return httpx.Response(
            200,
            json={
                "default": {
                    "timelineData": [
                        {
                            "time": "1756684800",
                            "formattedTime": "week",
                            "value": [40] + [20] * (len(keys) - 1),
                            "hasData": [not collapsed] + [not unmeasured] * (len(keys) - 1),
                        }
                    ]
                }
            },
        )

    router.get(EXPLORE_URL).mock(side_effect=explore)
    router.get(f"{WIDGETDATA_URL}/multiline").mock(side_effect=timeline)
    if widget_noise:
        router.get(f"{WIDGETDATA_URL}/comparedgeo").respond(500)
    return calls


def test_complete_uses_default_anchor_market_and_cached_batches(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with respx.mock(assert_all_called=True) as router:
        calls = mock_trends(router)
        result = run_demand(settings, ["Anchor", "a", "b", "c", "d", "e"])
        cached = run_demand(settings, ["Anchor", "a", "b", "c", "d", "e"])
    assert result.completeness is Completeness.COMPLETE
    assert result.data is not None
    assert result.data.anchor == "anchor"
    assert (result.data.batches_requested, result.data.batches_failed) == (2, 0)
    assert [(row.keyword, row.relative_demand) for row in result.data.rows] == [
        ("anchor", 100),
        ("a", 50),
        ("b", 50),
        ("c", 50),
        ("d", 50),
        ("e", 50),
    ]
    assert calls == [["anchor", "a", "b", "c", "d"], ["anchor", "e"]]
    assert result.to_wire() == cached.to_wire()
    assert result.data.caveats == [RELATIVE_CAVEAT, RESOLUTION_CAVEAT, STITCHING_CAVEAT]
    assert result.warnings == result.errors == []


def test_caveat_constants_keep_their_exact_meaning() -> None:
    expected = [
        "Values are relative to the anchor, not absolute search volumes.",
        "The scale is tied to the anchor; much weaker keywords hit the anchor's resolution limit.",
        "Batches are stitched through the anchor; stitching error accumulates from batch to batch.",
    ]
    caveats = [RELATIVE_CAVEAT, RESOLUTION_CAVEAT, STITCHING_CAVEAT]
    assert caveats == expected


def test_usecase_default_timeframe_reaches_data_and_http(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=True) as router:
        mock_trends(router)
        result = run_demand(settings_for(tmp_path), ["anchor", "other"])
        request = router.get(EXPLORE_URL).calls[0].request
    assert result.data is not None
    assert result.data.timeframe == "today 12-m"
    assert json.loads(request.url.params["req"])["comparisonItem"][0]["time"] == "today 12-m"


def test_cli_default_timeframe_reaches_data_and_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_main, "load_settings", lambda: settings_for(tmp_path))
    with respx.mock(assert_all_called=True) as router:
        mock_trends(router)
        result = CliRunner().invoke(cli_main.app, ["demand", "anchor", "other"])
        request = router.get(EXPLORE_URL).calls[0].request
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["timeframe"] == "today 12-m"
    assert json.loads(request.url.params["req"])["comparisonItem"][0]["time"] == "today 12-m"


def test_mcp_default_timeframe_reaches_data_and_http(tmp_path: Path) -> None:
    server = build_server(settings_for(tmp_path))

    async def call_demand() -> dict[str, object]:
        with anyio.fail_after(10):
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
                        "rank_keyword_demand", {"keywords": ["anchor", "other"]}
                    )
                task_group.cancel_scope.cancel()
        assert result.is_error is not True
        assert result.structured_content is not None
        return cast(dict[str, object], result.structured_content)

    with respx.mock(assert_all_called=True) as router:
        mock_trends(router)
        result = anyio.run(call_demand)
        request = router.get(EXPLORE_URL).calls[0].request
    assert result["completeness"] == "complete"
    data = result["data"]
    assert isinstance(data, dict)
    assert data["timeframe"] == "today 12-m"
    assert json.loads(request.url.params["req"])["comparisonItem"][0]["time"] == "today 12-m"


@pytest.mark.parametrize("failure", [429, 500, "network", "api"])
def test_failed_middle_batch_is_partial_and_later_batch_is_still_read(
    tmp_path: Path, failure: int | str
) -> None:
    with respx.mock(assert_all_called=True) as router:
        calls = mock_trends(router, failures={2: failure})
        result = run_demand(settings_for(tmp_path), [f"k{i}" for i in range(10)])
    assert result.completeness is Completeness.PARTIAL
    assert result.data is not None
    assert result.data.batches_failed == 1
    assert len(calls) == 3
    rows = {row.keyword: row for row in result.data.rows}
    assert rows["k9"].relative_demand == 50.0
    assert [rows[f"k{i}"].relative_demand for i in range(5, 9)] == [None] * 4
    assert rows["k5"].reason == result.errors[0]
    assert rows["k5"].batch == 2


def test_first_failed_batch_does_not_make_the_anchor_unknown(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=True) as router:
        mock_trends(router, failures={1: 500})
        result = run_demand(settings_for(tmp_path), [f"k{i}" for i in range(6)])
    assert result.data is not None
    anchor = next(row for row in result.data.rows if row.is_anchor)
    assert (anchor.relative_demand, anchor.batch) == (100.0, 2)
    assert result.completeness is Completeness.PARTIAL


def test_all_failed_batches_are_empty(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=False) as router:
        calls = mock_trends(router, failures={1: 500, 2: 500})
        result = run_demand(settings_for(tmp_path), [f"k{i}" for i in range(6)])
    assert result.completeness is Completeness.EMPTY
    assert result.data is not None
    assert result.data.batches_failed == result.data.batches_requested == 2
    assert all(row.relative_demand is None for row in result.data.rows)
    assert len(calls) == 2


def test_provider_unavailable_counts_every_batch(tmp_path: Path) -> None:
    with respx.mock:
        result = run_demand(
            Settings(data_dir=tmp_path, trends_enabled=False), [f"k{i}" for i in range(6)]
        )
    assert result.completeness is Completeness.EMPTY
    assert result.data is not None
    assert result.data.batches_failed == 2
    assert result.errors == ["Google Trends is disabled by configuration."] * 2


@pytest.mark.parametrize(
    "keywords, anchor, reason",
    [
        (["one"], None, "2"),
        ([], None, "2"),
        (["one", " ONE "], None, "2"),
        ([f"k{i}" for i in range(51)], None, "50"),
        (["one", "two"], "missing", "anchor"),
    ],
)
def test_refusals_are_empty_envelopes_without_http(
    tmp_path: Path, keywords: list[str], anchor: str | None, reason: str
) -> None:
    with respx.mock:
        result = run_demand(settings_for(tmp_path), keywords, anchor=anchor)
    assert result.completeness is Completeness.EMPTY
    assert result.data is None
    assert result.completeness_reason and reason in result.completeness_reason
    assert result.errors == [result.completeness_reason]


def test_explicit_anchor_normalization_and_market_reach_http(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=True) as router:
        calls = mock_trends(router)
        result = run_demand(
            settings_for(tmp_path),
            ["other", " Anchor ", "OTHER"],
            anchor="\uff21\uff2e\uff23\uff28\uff2f\uff32",
            language="ru",
            country="RU",
            timeframe="today 3-m",
        )
        request = router.calls[1].request
    assert calls == [["anchor", "other"]]
    assert request.url.params["hl"] == "ru"
    assert json.loads(request.url.params["req"])["comparisonItem"] == [
        {"keyword": "anchor", "geo": "RU", "time": "today 3-m"},
        {"keyword": "other", "geo": "RU", "time": "today 3-m"},
    ]
    assert result.data is not None
    assert (result.data.language, result.data.country, result.data.timeframe) == (
        "ru",
        "RU",
        "today 3-m",
    )


def test_other_widget_noise_does_not_downgrade_the_envelope(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=True) as router:
        mock_trends(router, widget_noise=True)
        result = run_demand(settings_for(tmp_path), ["anchor", "other"])
    assert result.completeness is Completeness.COMPLETE
    assert result.warnings == []
    assert result.data is not None
    assert len(result.data.notices) == 2
    assert result.data.notices[0].startswith("GEO_MAP:")
    assert result.data.notices[1].startswith("RELATED_QUERIES")


@pytest.mark.parametrize(
    "collapsed, expected", [(False, Completeness.PARTIAL), (True, Completeness.EMPTY)]
)
def test_no_measured_participant_is_partial_but_no_usable_anchor_is_empty(
    tmp_path: Path, collapsed: bool, expected: Completeness
) -> None:
    with respx.mock(assert_all_called=True) as router:
        mock_trends(router, unmeasured=True, collapsed=collapsed)
        result = run_demand(settings_for(tmp_path), ["anchor", "other"])
    assert result.completeness is expected
    assert result.data is not None
    assert result.data.batches_failed == 0
    assert result.data.rows[1].relative_demand is None


def test_timeseries_failure_keeps_its_reason_and_warning(tmp_path: Path) -> None:
    with respx.mock(assert_all_called=True) as router:
        mock_trends(router, timeline_failure=True, widget_noise=True)
        result = run_demand(settings_for(tmp_path), ["anchor", "other"])
    assert result.completeness is Completeness.EMPTY
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("TIMESERIES:")
    assert result.completeness_reason == result.warnings[0]
    assert result.data is not None
    assert all(row.reason == result.warnings[0] for row in result.data.rows)


@pytest.mark.parametrize("keywords", [[], ["only"]])
def test_cli_refusal_is_json_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keywords: list[str]
) -> None:
    monkeypatch.setattr(cli_main, "load_settings", lambda: settings_for(tmp_path))
    result = CliRunner().invoke(cli_main.app, ["demand", *keywords])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["data"] is None
    assert json.loads(result.stdout)["completeness"] == "empty"


def test_cli_table_and_notices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_main, "load_settings", lambda: settings_for(tmp_path))
    with respx.mock(assert_all_called=True) as router:
        mock_trends(router, widget_noise=True)
        result = CliRunner().invoke(
            cli_main.app, ["demand", "anchor", "other", "--format", "table"]
        )
    assert result.exit_code == 0
    assert "FIELD\tVALUE" in result.stdout
    assert '"relative_demand": 50.0' in result.stdout
    assert "RELATED_QUERIES" in result.stderr
