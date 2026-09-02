import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import structlog
from typer.testing import CliRunner

from google_keyword_ai.cli import main as cli_main
from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.expansion import ExpansionStrategy
from google_keyword_ai.logging import configure_logging
from google_keyword_ai.normalize import KeywordCandidate
from google_keyword_ai.providers.autocomplete import PRIMARY_ENDPOINT
from google_keyword_ai.providers.base import ProviderInfo
from google_keyword_ai.providers.expander import ExpansionLimits, ExpansionStats
from google_keyword_ai.providers.trends.models import TrendPoint, TrendsResult
from google_keyword_ai.usecases.doctor import run_doctor
from google_keyword_ai.usecases.expand import ExpandData
from google_keyword_ai.usecases.trends import TrendsData


def run_cli(
    tmp_path: Path, *arguments: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GKAI_DATA_DIR"] = str(tmp_path / "cli-data")
    if extra_env is not None:
        environment.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "google_keyword_ai.cli.main", *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_doctor_json_envelope(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "doctor", "--format", "json")

    assert result.returncode == 0, result.stderr
    payload: dict[str, Any] = json.loads(result.stdout)
    assert set(payload) >= {
        "schema_version",
        "data",
        "warnings",
        "errors",
        "completeness",
    }
    assert payload["schema_version"] == "1.0.0"
    assert payload["completeness"] == "complete"
    assert len(payload["data"]["providers"]) == 4
    autocomplete = next(
        provider for provider in payload["data"]["providers"] if provider["name"] == "autocomplete"
    )
    assert autocomplete == {"name": "autocomplete", "available": True, "detail": "ready"}
    assert result.stdout.count("\n") == 1


def test_stdout_clean_with_debug_logs(tmp_path: Path) -> None:
    result = run_cli(
        tmp_path,
        "doctor",
        "--format",
        "json",
        extra_env={"GKAI_LOG_LEVEL": "debug"},
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["data"]["database"] == "ok"
    assert result.stderr
    assert '"level": "debug"' in result.stderr


def test_config_show_masks_secrets(tmp_path: Path) -> None:
    token = "token-that-must-never-leak"
    result = run_cli(
        tmp_path,
        "config",
        "show",
        "--format",
        "json",
        extra_env={"GKAI_GOOGLE_ADS_DEVELOPER_TOKEN": token},
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["data"]["google_ads_developer_token"] == "***"
    assert token not in result.stdout
    assert token not in result.stderr


def test_doctor_does_not_raise_when_database_cannot_open(tmp_path: Path) -> None:
    file_instead_of_directory = tmp_path / "not-a-directory"
    file_instead_of_directory.write_text("occupied", encoding="utf-8")

    envelope = run_doctor(Settings(data_dir=file_instead_of_directory))

    assert envelope.completeness is Completeness.PARTIAL
    assert envelope.completeness_reason
    assert envelope.data.database != "ok"
    assert envelope.data.schema_version == 0


def test_cli_returns_one_for_partial_result(tmp_path: Path) -> None:
    file_instead_of_directory = tmp_path / "not-a-directory"
    file_instead_of_directory.write_text("occupied", encoding="utf-8")

    result = run_cli(
        tmp_path,
        "doctor",
        "--format",
        "json",
        extra_env={"GKAI_DATA_DIR": str(file_instead_of_directory)},
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["completeness"] == "partial"


def test_reconfigure_logging_does_not_duplicate_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("debug")
    configure_logging("debug")
    capsys.readouterr()

    structlog.get_logger("test").debug("one_event")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert json.loads(captured.err)["event"] == "one_event"


def test_suggest_prints_json_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(data_dir=tmp_path / "suggest-data", http_max_attempts=1)
    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)

    with respx.mock(assert_all_called=True) as router:
        router.get(
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
        result = CliRunner().invoke(
            cli_main.app,
            ["suggest", "seed", "--language", "en", "--country", "US", "--limit", "1"],
        )

    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = json.loads(result.stdout)
    assert payload["completeness"] == "complete"
    assert payload["data"]["suggestions"][0]["text"] == "seed one"


def test_third_party_logs_go_to_stderr_not_stdout(tmp_path: Path) -> None:
    """stdout carries protocol and JSON only.

    httpx logs every request through the standard library. Without our own
    handler something else installs one, the format diverges from structlog, and
    a misconfigured stream would corrupt both the MCP stdio protocol and the
    machine-readable CLI output.
    """
    import logging as stdlib_logging

    configure_logging("debug")
    httpx_logger = stdlib_logging.getLogger("httpx")
    assert httpx_logger.getEffectiveLevel() == stdlib_logging.WARNING
    handlers = stdlib_logging.getLogger().handlers
    assert handlers, "root logger must have a handler of ours"
    assert all(getattr(handler, "stream", sys.stderr) is sys.stderr for handler in handlers), (
        "every handler must write to stderr"
    )


def test_expand_prints_json_envelope_and_forwards_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "expand-data")
    captured: dict[str, object] = {}

    def fake_run_expand(
        active_settings: Settings,
        seed: str,
        **kwargs: object,
    ) -> Envelope[ExpandData]:
        captured.update(kwargs)
        captured["settings"] = active_settings
        captured["seed"] = seed
        return Envelope(
            data=ExpandData(
                seed=seed,
                language="en",
                country="US",
                provider=ProviderInfo(
                    name="autocomplete",
                    official=False,
                    stability="unofficial",
                ),
                strategies=["digits", "modifiers"],
                limits=ExpansionLimits(
                    max_depth=2,
                    max_queries=20,
                    max_results=30,
                    max_runtime_seconds=4,
                ),
                stats=ExpansionStats(queries_executed=3, depth_reached=1),
                keywords=[
                    KeywordCandidate(
                        raw="seed one",
                        normalized="seed one",
                        discovered_from=["autocomplete:digits:seed 1"],
                    )
                ],
            )
        )

    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    monkeypatch.setattr(cli_main, "run_expand", fake_run_expand)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "expand",
            "seed",
            "--language",
            "en",
            "--country",
            "US",
            "--depth",
            "2",
            "--max-queries",
            "20",
            "--max-results",
            "30",
            "--max-runtime",
            "4",
            "--strategy",
            "digits",
            "--strategy",
            "modifiers",
            "--limit",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = json.loads(result.stdout)
    assert payload["data"]["keywords"][0]["normalized"] == "seed one"
    assert captured["seed"] == "seed"
    assert captured["strategies"] == [
        ExpansionStrategy.DIGITS,
        ExpansionStrategy.MODIFIERS,
    ]
    assert captured["max_runtime_seconds"] == 4.0


def _trends_envelope(keywords: list[str]) -> Envelope[TrendsData]:
    return Envelope(
        data=TrendsData(
            provider=ProviderInfo(name="trends", official=False, stability="unofficial"),
            result=TrendsResult(
                keywords=keywords,
                geo="US",
                timeframe="now 7-d",
                normalization_scope="0123456789abcdef",
                timeline=[
                    TrendPoint(
                        timestamp=datetime(2026, 9, 1, tzinfo=UTC),
                        formatted_time="Sep 1, 2026",
                        values=[100],
                        has_data=[True],
                    )
                ],
                retrieved_at=datetime(2026, 9, 2, tzinfo=UTC),
                source="https://trends.google.com/trends/api/explore",
            ),
        )
    )


def test_trends_prints_json_and_forwards_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "trends-data")
    captured: dict[str, object] = {}

    def fake_run_trends(
        active_settings: Settings, keyword: str, **kwargs: object
    ) -> Envelope[TrendsData]:
        captured.update(kwargs)
        captured["settings"] = active_settings
        captured["keyword"] = keyword
        return _trends_envelope([keyword])

    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    monkeypatch.setattr(cli_main, "run_trends", fake_run_trends)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "trends",
            "keyword",
            "--language",
            "en",
            "--country",
            "US",
            "--timeframe",
            "now 7-d",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["result"]["keywords"] == ["keyword"]
    assert captured == {
        "settings": settings,
        "keyword": "keyword",
        "language": "en",
        "country": "US",
        "timeframe": "now 7-d",
    }


def test_trends_compare_sends_keywords_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "trends-compare-data")
    captured: dict[str, object] = {}

    def fake_run_compare(
        active_settings: Settings, keywords: list[str], **kwargs: object
    ) -> Envelope[TrendsData]:
        captured.update(kwargs)
        captured["settings"] = active_settings
        captured["keywords"] = keywords
        return _trends_envelope(keywords)

    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    monkeypatch.setattr(cli_main, "run_trends_compare", fake_run_compare)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "trends",
            "compare",
            "one",
            "two",
            "--language",
            "en",
            "--country",
            "US",
            "--timeframe",
            "now 7-d",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["result"]["keywords"] == ["one", "two"]
    assert captured["keywords"] == ["one", "two"]
    assert captured["timeframe"] == "now 7-d"


def test_doctor_reports_trends_disabled_by_kill_switch(tmp_path: Path) -> None:
    result = run_cli(
        tmp_path,
        "doctor",
        "--format",
        "json",
        extra_env={"GKAI_TRENDS_ENABLED": "false"},
    )

    assert result.returncode == 0, result.stderr
    providers = json.loads(result.stdout)["data"]["providers"]
    trends = next(provider for provider in providers if provider["name"] == "trends")
    assert trends == {
        "name": "trends",
        "available": False,
        "detail": "disabled by configuration",
    }
