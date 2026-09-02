import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import anyio
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from google_keyword_ai.config import Settings
from google_keyword_ai.mcp.server import build_server


def test_cli_and_mcp_doctor_have_identical_wire_envelopes(tmp_path: Path) -> None:
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


def test_doctor_tool_is_synchronous_so_the_sdk_offloads_it() -> None:
    """Blocking work must not run on the event loop.

    The SDK routes sync tool functions through ``anyio.to_thread.run_sync`` and
    awaits async ones directly on the loop. ``run_doctor`` opens SQLite and later
    milestones add network and gRPC calls, so the tool has to stay synchronous.
    """
    server = build_server(Settings())
    tool = server._tool_manager.get_tool("doctor")
    assert tool is not None
    assert tool.is_async is False
