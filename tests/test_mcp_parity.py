import json
import os
import subprocess
import sys
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
from google_keyword_ai.mcp.server import build_server
from google_keyword_ai.providers.autocomplete import PRIMARY_ENDPOINT


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
