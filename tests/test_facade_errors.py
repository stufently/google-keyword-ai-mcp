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


def test_a_refused_argument_is_the_same_envelope_on_both_facades(
    thread_offload: None, tmp_path: Path
) -> None:
    """One envelope on both facades is the contract, refusals included.

    A protocol error would also carry the reason, but it makes the caller parse
    two shapes for one outcome: an envelope when the run is missing, a tool
    error when the limit is. The CLI prints exactly one shape for both, and
    this pins that MCP does too -- byte for byte, not merely in spirit.
    """
    result = call_tool(tmp_path, "score_run", {"run_id": "run_missing", "limit": 0})

    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["completeness_reason"] == "Score limit must be positive."

    environment = os.environ.copy()
    environment["GKAI_DATA_DIR"] = str(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "google_keyword_ai.cli.main",
            "score",
            "run_missing",
            "--limit",
            "0",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1, completed.stderr
    assert result.structured_content == json.loads(completed.stdout)


def test_every_tool_publishes_a_payload_that_admits_the_refusal_envelope(
    thread_offload: None, tmp_path: Path
) -> None:
    """A tool that cannot express `data: null` cannot report its own refusal.

    The SDK validates a return value against the declared type, so the guard's
    envelope would be rejected as a crash by any tool whose payload is not
    nullable -- which is how the analysis tools lost their empty answer in the
    first place. Checking every tool catches the one added later.
    """
    for name, tool in list_tools(tmp_path).items():
        schema = tool.output_schema
        assert schema is not None, name
        payload = schema["properties"]["data"]
        assert "anyOf" in payload, f"tool {name} declares a payload that cannot be null"
        assert {"type": "null"} in payload["anyOf"], (
            f"tool {name} cannot return the envelope its own guard produces"
        )


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        (["score", "run_missing", "--limit", "0"], "Score limit must be positive."),
        (["research", "alpha", "--limit", "0"], "Research limit must be positive."),
        (["research", "alpha", "--scenario", "nonsense"], "Unknown research scenario: nonsense."),
        (["expand", "alpha", "--limit", "0"], "Expansion limit must be positive."),
        (["suggest", "alpha", "--limit", "0"], "Suggestion limit must be positive."),
        (
            ["ads", "ideas", "alpha", "--limit", "0"],
            "Keyword idea limit must be positive.",
        ),
        (
            ["gsc", "queries", "https://example.com/", "--limit", "0"],
            "Query limit must be positive.",
        ),
        (
            ["gsc", "opportunities", "https://example.com/", "--limit", "0"],
            "Opportunity limit must be positive.",
        ),
        (
            ["competitor", "http://["],
            "Target is not a valid URL or domain.",
        ),
        (
            ["research", "http://[", "--scenario", "competitor"],
            "Target is not a valid URL or domain.",
        ),
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


@pytest.mark.parametrize(
    ("variable", "value", "fragment"),
    [
        ("GKAI_LOG_LEVEL", "invalid", "Unknown log level: invalid."),
        ("GKAI_HTTP_MAX_ATTEMPTS", "abc", "http_max_attempts"),
        ("GKAI_HTTP_MAX_ATTEMPTS", "0", "http_max_attempts must be at least 1."),
    ],
)
def test_an_unusable_configuration_is_reported_in_the_envelope_too(
    tmp_path: Path, variable: str, value: str, fragment: str
) -> None:
    """Settings load before any command builds its envelope, and can refuse.

    Reading the configuration and configuring logging both happen ahead of the
    command, so an error there used to escape the guard entirely and exit 1
    with nothing on stdout. A value of the wrong type arrives as a pydantic
    error rather than one of ours, which is the same contract break wearing a
    different exception.
    """
    environment = os.environ.copy()
    environment["GKAI_DATA_DIR"] = str(tmp_path)
    environment[variable] = value
    completed = subprocess.run(
        [sys.executable, "-m", "google_keyword_ai.cli.main", "doctor"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1, completed.stderr
    assert "Traceback" not in completed.stderr
    envelope = cast(dict[str, object], json.loads(completed.stdout))
    assert envelope["completeness"] == "empty"
    assert fragment in cast(str, envelope["completeness_reason"])


def test_an_unusable_configuration_never_echoes_the_value_it_rejected(
    tmp_path: Path,
) -> None:
    """A rejected setting can be a credential, so the message names the field only."""
    environment = os.environ.copy()
    environment["GKAI_DATA_DIR"] = str(tmp_path)
    environment["GKAI_GOOGLE_ADS_DEVELOPER_TOKEN"] = "s3cret-token"
    environment["GKAI_HTTP_MAX_ATTEMPTS"] = "not-a-number"
    completed = subprocess.run(
        [sys.executable, "-m", "google_keyword_ai.cli.main", "doctor"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1
    assert "s3cret-token" not in completed.stdout
    assert "s3cret-token" not in completed.stderr
    assert "not-a-number" not in completed.stdout


def test_an_unknown_strategy_over_mcp_is_an_envelope_not_a_crash(
    thread_offload: None, tmp_path: Path
) -> None:
    """MCP publishes strategies as free strings, so the check belongs in the core.

    The CLI gets an enum from Typer and can never reach this, but an MCP
    caller hands over whatever it likes. `ExpansionStrategy("nonsense")`
    raises a bare `ValueError`, which the guard does not recognise as a
    refusal -- so a typo came back as `Error executing tool expand_keywords`
    with nothing to say what was wrong.
    """
    result = call_tool(
        tmp_path,
        "expand_keywords",
        {"seed": "alpha", "strategies": ["nonsense"]},
    )

    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["completeness"] == "empty"
    reason = cast(str, result.structured_content["completeness_reason"])
    assert reason.startswith("Unknown expansion strategy.")
    assert "suffix_alphabet" in reason


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("analyze_competitor", {"target": "http://["}),
        ("research_keywords", {"target": "http://[", "scenario": "competitor"}),
    ],
)
def test_a_malformed_target_over_mcp_is_an_envelope_not_a_crash(
    thread_offload: None,
    tmp_path: Path,
    name: str,
    arguments: dict[str, object],
) -> None:
    """A netloc with an unclosed bracket is a typo, not a crash in the tool.

    `urlsplit("http://[")` raises a bare `ValueError`, which the guard does not
    recognise as a refusal: MCP answered `Error executing tool` with nothing to
    say what was wrong, and the CLI printed a traceback instead of the envelope
    it documents for exit 1. Both entry points that split a target are pinned,
    because the two lines that did the splitting were copies of each other.
    """
    result = call_tool(tmp_path, name, arguments)

    assert result.is_error is not True
    assert result.structured_content is not None
    assert result.structured_content["completeness"] == "empty"
    assert result.structured_content["completeness_reason"] == (
        "Target is not a valid URL or domain."
    )
