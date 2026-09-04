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
from google_keyword_ai.opportunities import Opportunity
from google_keyword_ai.pipeline.budget import BudgetSpend
from google_keyword_ai.pipeline.models import (
    DataQuality,
    DryRunPlan,
    ResearchData,
    ResearchKeyword,
    ResearchStats,
    SourceUsage,
)
from google_keyword_ai.providers.autocomplete import PRIMARY_ENDPOINT
from google_keyword_ai.providers.base import ProviderInfo
from google_keyword_ai.providers.expander import ExpansionLimits, ExpansionStats
from google_keyword_ai.providers.google_ads import KeywordIdea, KeywordMetrics
from google_keyword_ai.providers.search_console import SearchAnalyticsRow, SiteProperty
from google_keyword_ai.providers.trends.models import TrendPoint, TrendsResult
from google_keyword_ai.usecases.ads import AdsData
from google_keyword_ai.usecases.doctor import run_doctor
from google_keyword_ai.usecases.expand import ExpandData
from google_keyword_ai.usecases.gsc import OpportunitiesData, PropertiesData, QueriesData
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


def test_research_dry_run_prints_plan_without_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "research-data")
    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "research",
            "running shoes",
            "--dry-run",
            "--max-keywords",
            "40",
            "--max-autocomplete-queries",
            "10",
            "--max-ads-calls",
            "2",
            "--max-trends-calls",
            "1",
            "--max-runtime",
            "30",
        ],
    )

    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = json.loads(result.stdout)
    assert payload["data"]["scenario"] == "niche"
    assert payload["data"]["estimated_autocomplete_queries"] == 10


def test_run_list_prints_saved_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(data_dir=tmp_path / "run-list")
    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    monkeypatch.setattr(cli_main, "run_list", lambda active, limit=20: Envelope(data=[]))

    result = CliRunner().invoke(cli_main.app, ["run", "list", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"] == []


def test_research_save_run_flag_is_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "research-save")
    captured: dict[str, object] = {}

    def fake_run_research(
        active_settings: Settings,
        target: str,
        **kwargs: object,
    ) -> Envelope[dict[str, object]]:
        captured["settings"] = active_settings
        captured["target"] = target
        captured.update(kwargs)
        return Envelope(data={"saved": True}, run_id="run_0123456789abcdef0123456789")

    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    monkeypatch.setattr(cli_main, "run_research", fake_run_research)

    result = CliRunner().invoke(cli_main.app, ["research", "topic", "--save-run"])

    assert result.exit_code == 0, result.output
    assert captured["save_run"] is True
    assert json.loads(result.stdout)["run_id"] == "run_0123456789abcdef0123456789"


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


def _ads_envelope(mode: str) -> Envelope[AdsData]:
    return Envelope(
        data=AdsData(
            provider=ProviderInfo(name="google_ads", official=True, stability="stable"),
            mode=mode,
            language="en",
            country="US",
            ideas=[
                KeywordIdea(
                    text="keyword idea",
                    metrics=KeywordMetrics(
                        avg_monthly_searches=100,
                        competition="MEDIUM",
                    ),
                )
            ],
        )
    )


def test_ads_ideas_cli_forwards_seed_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "ads-ideas")
    captured: dict[str, object] = {}

    def fake_run_ads_ideas(
        active_settings: Settings,
        keywords: list[str] | None,
        **kwargs: object,
    ) -> Envelope[AdsData]:
        captured["settings"] = active_settings
        captured["keywords"] = keywords
        captured.update(kwargs)
        return _ads_envelope("keyword_and_url_seed")

    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    monkeypatch.setattr(cli_main, "run_ads_ideas", fake_run_ads_ideas)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "ads",
            "ideas",
            "seed",
            "--url",
            "https://example.com/page",
            "--include-adult",
            "--limit",
            "1",
            "--language",
            "en",
            "--country",
            "US",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["ideas"][0]["text"] == "keyword idea"
    assert captured["keywords"] == ["seed"]
    assert captured["url"] == "https://example.com/page"
    assert captured["include_adult"] is True
    assert captured["limit"] == 1


def test_ads_historical_cli_forwards_keywords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "ads-historical")
    captured: dict[str, object] = {}

    def fake_run_historical(
        active_settings: Settings,
        keywords: list[str],
        **kwargs: object,
    ) -> Envelope[AdsData]:
        captured["settings"] = active_settings
        captured["keywords"] = keywords
        captured.update(kwargs)
        return _ads_envelope("historical_metrics")

    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    monkeypatch.setattr(cli_main, "run_ads_historical", fake_run_historical)

    result = CliRunner().invoke(
        cli_main.app,
        ["ads", "historical", "one", "two", "--language", "en", "--country", "US"],
    )

    assert result.exit_code == 0, result.output
    assert captured["keywords"] == ["one", "two"]
    assert captured["language"] == "en"


def test_competitor_cli_forwards_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(data_dir=tmp_path / "competitor")
    captured: dict[str, object] = {}

    def fake_run_competitor(
        active_settings: Settings,
        target: str,
        **kwargs: object,
    ) -> Envelope[AdsData]:
        captured["settings"] = active_settings
        captured["target"] = target
        captured.update(kwargs)
        return _ads_envelope("keyword_and_url_seed")

    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    monkeypatch.setattr(cli_main, "run_competitor", fake_run_competitor)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "competitor",
            "https://example.com/page",
            "--seed-keyword",
            "seed",
            "--limit",
            "5",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["target"] == "https://example.com/page"
    assert captured["seed_keyword"] == "seed"
    assert captured["limit"] == 5


def test_gsc_properties_cli_uses_properties_usecase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "gsc-properties")
    expected = Envelope(
        data=PropertiesData(
            provider=ProviderInfo(name="search_console", official=True, stability="stable"),
            properties=[
                SiteProperty(
                    site_url="sc-domain:example.com",
                    permission_level="siteOwner",
                )
            ],
        )
    )
    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    monkeypatch.setattr(cli_main, "run_gsc_properties", lambda _settings: expected)

    result = CliRunner().invoke(cli_main.app, ["gsc", "properties"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == expected.to_wire()


def test_gsc_queries_cli_forwards_all_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "gsc-queries")
    captured: dict[str, object] = {}
    expected = Envelope(
        data=QueriesData(
            provider=ProviderInfo(name="search_console", official=True, stability="stable"),
            site_url="sc-domain:example.com",
            start_date="2026-08-01",
            end_date="2026-08-02",
            dimensions=["query", "page"],
            rows=[
                SearchAnalyticsRow(
                    keys={"query": "keyword", "page": "https://example.com/page"},
                    clicks=1,
                    impressions=100,
                    ctr=0.01,
                    position=8,
                )
            ],
            truncated=False,
            truncation_reason=None,
        )
    )

    def fake_run(
        active_settings: Settings, site_url: str, **kwargs: object
    ) -> Envelope[QueriesData]:
        captured["settings"] = active_settings
        captured["site_url"] = site_url
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    monkeypatch.setattr(cli_main, "run_gsc_queries", fake_run)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "gsc",
            "queries",
            "sc-domain:example.com",
            "--days",
            "7",
            "--start-date",
            "2026-08-01",
            "--end-date",
            "2026-08-02",
            "--dimension",
            "query",
            "--dimension",
            "page",
            "--country",
            "US",
            "--search-type",
            "image",
            "--limit",
            "10",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["site_url"] == "sc-domain:example.com"
    assert captured["days"] == 7
    assert captured["dimensions"] == ["query", "page"]
    assert captured["country"] == "US"
    assert captured["search_type"] == "image"
    assert captured["limit"] == 10


def test_gsc_opportunities_cli_forwards_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "gsc-opportunities")
    captured: dict[str, object] = {}
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
        active_settings: Settings, site_url: str, **kwargs: object
    ) -> Envelope[OpportunitiesData]:
        captured["settings"] = active_settings
        captured["site_url"] = site_url
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    monkeypatch.setattr(cli_main, "run_gsc_opportunities", fake_run)

    result = CliRunner().invoke(
        cli_main.app,
        [
            "gsc",
            "opportunities",
            "sc-domain:example.com",
            "--days",
            "14",
            "--country",
            "US",
            "--limit",
            "3",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["site_url"] == "sc-domain:example.com"
    assert captured["days"] == 14
    assert captured["country"] == "US"
    assert captured["limit"] == 3


def test_analysis_commands_are_registered() -> None:
    result = CliRunner().invoke(cli_main.app, ["--help"])
    assert result.exit_code == 0
    for command in ("score", "cluster", "explain-score", "niche", "keyword"):
        assert command in result.output
    assert CliRunner().invoke(cli_main.app, ["niche", "--help"]).exit_code == 0
    assert CliRunner().invoke(cli_main.app, ["keyword", "--help"]).exit_code == 0


def _research_payload() -> ResearchData:
    return ResearchData(
        scenario="topic",
        input="alpha",
        language="en",
        country="US",
        keywords=[
            ResearchKeyword(
                keyword="alpha keyword tool",
                normalized="alpha keyword tool",
                discovered_from=["autocomplete"],
            )
        ],
        stats=ResearchStats(spend=BudgetSpend()),
        data_quality=DataQuality(
            sources=[SourceUsage(name="autocomplete", used=True, available=True, detail="used")],
            retrieved_at=datetime.now(UTC),
            absolute_metrics=[],
            relative_metrics=[],
            derived_metrics=[],
            caveats=["Test caveat."],
        ),
    )


def test_research_markdown_renders_report(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    data = ResearchData(
        scenario="topic",
        input="alpha",
        language="en",
        country="US",
        keywords=[
            ResearchKeyword(
                keyword="alpha keyword tool",
                normalized="alpha keyword tool",
                discovered_from=["autocomplete"],
            )
        ],
        stats=ResearchStats(spend=BudgetSpend()),
        data_quality=DataQuality(
            sources=[SourceUsage(name="autocomplete", used=True, available=True, detail="used")],
            retrieved_at=now,
            absolute_metrics=[],
            relative_metrics=[],
            derived_metrics=[],
            caveats=["Test caveat."],
        ),
    )
    monkeypatch.setattr(cli_main, "load_settings", Settings)
    monkeypatch.setattr(cli_main, "run_research", lambda *_args, **_kwargs: Envelope(data=data))
    result = CliRunner().invoke(cli_main.app, ["research", "alpha", "--format", "markdown"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("# Keyword research\n")
    assert "## Data quality and limitations" in result.output


def test_trends_on_the_word_compare_is_not_a_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`compare` opens the subcommand only when something follows it.

    A lone `gkai trends compare` is a request for Trends on the word "compare",
    and the MCP tool answers it as one. The CLI routed it into the comparison
    with an empty keyword list and refused — the same input succeeding on one
    facade and failing on the other.
    """
    settings = Settings(data_dir=tmp_path / "compare")
    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    seen: list[str] = []

    def fake_run_trends(_settings: Settings, keyword: str, **_kwargs: object) -> Envelope[object]:
        seen.append(keyword)
        return Envelope(data={"ok": True})

    monkeypatch.setattr(cli_main, "run_trends", fake_run_trends)

    result = CliRunner().invoke(cli_main.app, ["trends", "compare"])

    assert result.exit_code == 0, result.output
    assert seen == ["compare"]


def test_markdown_on_a_dry_run_is_a_refusal_envelope_not_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit 2 promises that nothing ran; by this point the dry run already has.

    A combination of options the command cannot honour is a refusal like any
    other, so it travels in the envelope and exits 1 — the code the documented
    contract reserves for a verdict that still prints valid output.
    """
    settings = Settings(data_dir=tmp_path / "markdown")
    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    monkeypatch.setattr(
        cli_main,
        "run_research",
        lambda *_args, **_kwargs: Envelope(
            data=DryRunPlan(
                scenario="niche",
                steps=["step"],
                estimated_autocomplete_queries=1,
                estimated_ads_calls=0,
                estimated_trends_calls=0,
                sources=[],
            )
        ),
    )

    result = CliRunner().invoke(
        cli_main.app, ["research", "topic", "--dry-run", "--format", "markdown"]
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["completeness"] == "empty"
    assert "--dry-run" in payload["completeness_reason"]


def test_a_markdown_report_still_says_what_was_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report renders `data` and knows nothing of the envelope around it.

    Exiting 1 behind a document that reads as complete leaves the reason on no
    channel at all, while the skill's instruction is to tell the user what is
    absent using `completeness_reason`, `warnings` and `errors`.
    """
    settings = Settings(data_dir=tmp_path / "partial")
    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    monkeypatch.setattr(
        cli_main,
        "run_research",
        lambda *_args, **_kwargs: Envelope(
            data=_research_payload(),
            warnings=["Google Ads is unavailable; absolute search metrics are omitted."],
            completeness=Completeness.PARTIAL,
            completeness_reason="Google Ads is unavailable; absolute search metrics are omitted.",
        ),
    )

    result = CliRunner().invoke(cli_main.app, ["research", "topic", "--format", "markdown"])

    assert result.exit_code == 1
    assert "# Keyword research" in result.stdout
    diagnosis = json.loads(result.stderr)
    assert diagnosis["completeness"] == "partial"
    assert "Google Ads is unavailable" in diagnosis["completeness_reason"]
    assert diagnosis["warnings"]
