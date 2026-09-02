from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness
from google_keyword_ai.errors import AuthenticationError
from google_keyword_ai.providers.search_console import (
    SearchAnalyticsPage,
    SearchAnalyticsRow,
    SearchConsoleProvider,
)
from google_keyword_ai.usecases import gsc


def _settings(tmp_path: Path) -> Settings:
    credentials = tmp_path / "credentials.json"
    credentials.write_text("{}", encoding="utf-8")
    return Settings(data_dir=tmp_path / "data", search_console_credentials_path=credentials)


def _page(*, truncated: bool = False) -> SearchAnalyticsPage:
    return SearchAnalyticsPage(
        rows=[
            SearchAnalyticsRow(
                keys={"query": "keyword", "page": "https://example.com/page"},
                clicks=2,
                impressions=200,
                ctr=0.01,
                position=8,
            )
        ],
        truncated=truncated,
        truncation_reason="daily cap reached" if truncated else None,
    )


def test_success_envelope_contains_rows_and_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_query(
        self: SearchConsoleProvider, site_url: str, **kwargs: object
    ) -> SearchAnalyticsPage:
        assert site_url == "sc-domain:example.com"
        assert kwargs["dimensions"] == ["query"]
        return _page()

    monkeypatch.setattr(SearchConsoleProvider, "query", fake_query)

    result = gsc.run_gsc_queries(
        _settings(tmp_path),
        "sc-domain:example.com",
        start_date="2026-08-01",
        end_date="2026-08-02",
    )

    assert result.completeness is Completeness.COMPLETE
    assert result.data.provider.name == "search_console"
    assert result.data.rows[0].keys["query"] == "keyword"
    assert result.data.start_date == "2026-08-01"
    assert result.data.end_date == "2026-08-02"


def test_missing_credentials_returns_empty_without_raising(tmp_path: Path) -> None:
    result = gsc.run_gsc_queries(
        Settings(data_dir=tmp_path / "data"),
        "sc-domain:example.com",
        start_date="2026-08-01",
        end_date="2026-08-02",
    )

    assert result.completeness is Completeness.EMPTY
    assert result.errors
    assert result.data.rows == []


def test_provider_error_returns_empty_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_query(
        self: SearchConsoleProvider, site_url: str, **kwargs: object
    ) -> SearchAnalyticsPage:
        raise AuthenticationError("bad credentials")

    monkeypatch.setattr(SearchConsoleProvider, "query", fake_query)

    result = gsc.run_gsc_queries(
        _settings(tmp_path),
        "sc-domain:example.com",
        start_date="2026-08-01",
        end_date="2026-08-02",
    )

    assert result.completeness is Completeness.EMPTY
    assert result.errors == ["bad credentials"]
    assert result.completeness_reason == "bad credentials"


def test_partial_envelope_for_truncated_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_query(
        self: SearchConsoleProvider, site_url: str, **kwargs: object
    ) -> SearchAnalyticsPage:
        return _page(truncated=True)

    monkeypatch.setattr(SearchConsoleProvider, "query", fake_query)

    result = gsc.run_gsc_queries(
        _settings(tmp_path),
        "sc-domain:example.com",
        start_date="2026-08-01",
        end_date="2026-08-02",
    )

    assert result.completeness is Completeness.PARTIAL
    assert result.data.truncated is True
    assert result.warnings == ["daily cap reached"]
    assert result.completeness_reason == "daily cap reached"


def test_empty_response_has_explicit_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_query(
        self: SearchConsoleProvider, site_url: str, **kwargs: object
    ) -> SearchAnalyticsPage:
        return SearchAnalyticsPage(rows=[], truncated=False, truncation_reason=None)

    monkeypatch.setattr(SearchConsoleProvider, "query", fake_query)

    result = gsc.run_gsc_queries(
        _settings(tmp_path),
        "sc-domain:example.com",
        start_date="2026-08-01",
        end_date="2026-08-02",
    )

    assert result.completeness is Completeness.EMPTY
    assert result.errors == []
    assert result.completeness_reason == "no search analytics data"


def test_default_window_ends_day_before_yesterday(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object | None = None) -> "FrozenDateTime":
            assert tz is UTC
            return cls(2026, 9, 2, tzinfo=UTC)

    async def fake_query(
        self: SearchConsoleProvider, site_url: str, **kwargs: object
    ) -> SearchAnalyticsPage:
        captured.update(kwargs)
        return _page()

    monkeypatch.setattr(gsc, "datetime", FrozenDateTime)
    monkeypatch.setattr(SearchConsoleProvider, "query", fake_query)

    result = gsc.run_gsc_queries(_settings(tmp_path), "sc-domain:example.com", days=3)

    assert captured["start_date"] == date(2026, 8, 29)
    assert captured["end_date"] == date(2026, 8, 31)
    assert result.data.end_date == "2026-08-31"


def test_opportunities_use_query_and_page_dimensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    async def fake_query(
        self: SearchConsoleProvider, site_url: str, **kwargs: object
    ) -> SearchAnalyticsPage:
        captured.update(kwargs)
        return _page()

    monkeypatch.setattr(SearchConsoleProvider, "query", fake_query)

    result = gsc.run_gsc_opportunities(_settings(tmp_path), "sc-domain:example.com", limit=1)

    assert captured["dimensions"] == ["query", "page"]
    assert result.data.opportunities[0].kind == "quick_win"
    assert result.data.thresholds["min_impressions"] == 100
