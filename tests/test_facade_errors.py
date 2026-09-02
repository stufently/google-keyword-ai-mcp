"""Both facades promise an envelope. These pin what happens when work is refused.

The CLI documents exit 1 as a verdict about the data that still prints valid
JSON, and `docs/mcp.md` promises the tools return the same envelope the CLI
prints. A raised `GkaiError` used to break both promises at once: the CLI
exited 1 with an empty stdout and a traceback, and MCP answered `is_error`
with the reason stripped out.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import CallToolResult, Tool

from google_keyword_ai.config import Settings
from google_keyword_ai.mcp.server import build_server

NOT_FOUND_TOOLS = [
    ("score_run", {"run_id": "run_missing"}),
    ("cluster_run", {"run_id": "run_missing"}),
    ("explain_score", {"run_id": "run_missing", "keyword": "alpha"}),
    ("analyze_niche", {"run_id": "run_missing"}),
    ("inspect_keyword", {"run_id": "run_missing", "keyword": "alpha"}),
]


def call_tool(data_dir: Path, name: str, arguments: dict[str, object]) -> CallToolResult:
    server = build_server(Settings(data_dir=data_dir))

    async def exercise() -> CallToolResult:
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
                    raise_exceptions=False,
                )

            task_group.start_soon(run_server)
            async with ClientSession(client_read, client_write) as client:
                await client.initialize()
                result = await client.call_tool(name, arguments)
            task_group.cancel_scope.cancel()
        return result

    return anyio.run(exercise)


@pytest.mark.parametrize(("name", "arguments"), NOT_FOUND_TOOLS)
def test_a_missing_run_is_an_answer_over_mcp_not_a_tool_failure(
    thread_offload: None,
    tmp_path: Path,
    name: str,
    arguments: dict[str, object],
) -> None:
    """A run that does not exist is an ordinary empty result with a reason.

    The SDK validates a tool's return value against its declared type, so a
    tool typed `Envelope[NicheData]` cannot carry `data: null`. That turned
    every not-found answer into `Error executing tool <name>` with the reason
    stripped, leaving the caller unable to tell a missing run from a broken
    server.
    """
    result = call_tool(tmp_path, name, arguments)

    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["completeness"] == "empty"
    assert result.structured_content["completeness_reason"] == "Run run_missing was not found."
    assert result.structured_content["data"] is None


def test_a_refused_argument_reaches_the_mcp_caller_with_its_reason(
    thread_offload: None, tmp_path: Path
) -> None:
    """`ToolError` is the SDK's word for a failure the tool saw coming.

    Any other exception is treated as a crash and the message is withheld, so
    letting `GkaiError` escape told the caller only which tool failed.
    """
    result = call_tool(tmp_path, "score_run", {"run_id": "run_missing", "limit": 0})

    assert result.is_error is True
    rendered = " ".join(getattr(item, "text", "") for item in result.content)
    assert "Score limit must be positive." in rendered


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        (["score", "run_missing", "--limit", "0"], "Score limit must be positive."),
        (["research", "alpha", "--limit", "0"], "Research limit must be positive."),
        (["research", "alpha", "--scenario", "nonsense"], "Unknown research scenario: nonsense."),
    ],
)
def test_a_refused_request_still_prints_an_envelope_and_exits_one(
    tmp_path: Path, arguments: list[str], reason: str
) -> None:
    """Exit 1 is documented as a verdict that still printed valid JSON.

    A caller told to parse stdout on exit 1 has nothing to parse when the error
    escapes as a traceback, and cannot tell a rejected argument from a crash.
    """
    environment = os.environ.copy()
    environment["GKAI_DATA_DIR"] = str(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-m", "google_keyword_ai.cli.main", *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1, completed.stderr
    envelope = cast(dict[str, object], json.loads(completed.stdout))
    assert envelope["completeness"] == "empty"
    assert envelope["completeness_reason"] == reason
    assert envelope["errors"] == [reason]
    assert "Traceback" not in completed.stderr


def list_tools(data_dir: Path) -> dict[str, Tool]:
    server = build_server(Settings(data_dir=data_dir))

    async def exercise() -> dict[str, Tool]:
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
                    raise_exceptions=False,
                )

            task_group.start_soon(run_server)
            async with ClientSession(client_read, client_write) as client:
                await client.initialize()
                listing = await client.list_tools()
            task_group.cancel_scope.cancel()
        return {tool.name: tool for tool in listing.tools}

    return anyio.run(exercise)


def test_the_error_guard_leaves_the_published_schemas_alone(
    thread_offload: None, tmp_path: Path
) -> None:
    """The guard wraps every tool, and a wrapper is exactly how schemas get lost.

    The SDK reads a tool's arguments from the function signature and its output
    from the return annotation. A wrapper that hid either would publish a tool
    taking no arguments, and nothing else in the suite states that the
    parameters survived.
    """
    tools = list_tools(tmp_path)

    assert len(tools) == 14
    suggest = tools["suggest_keywords"]
    assert sorted(suggest.input_schema["properties"]) == ["country", "language", "limit", "query"]
    assert suggest.input_schema["required"] == ["query"]

    score = tools["score_run"]
    assert sorted(score.input_schema["properties"]) == ["limit", "run_id"]
    output = score.output_schema
    assert output is not None
    assert output["properties"]["data"]["anyOf"][-1] == {"type": "null"}, (
        "the published schema has to admit the empty answer the tool can return"
    )
