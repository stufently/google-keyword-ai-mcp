from pathlib import Path

import pytest

from google_keyword_ai.config import Settings
from google_keyword_ai.errors import InvalidConfigurationError
from google_keyword_ai.opportunities import find_opportunities
from google_keyword_ai.providers.search_console import SearchAnalyticsRow


def _row(
    query: str,
    *,
    impressions: int,
    position: float,
    ctr: float,
    clicks: int = 1,
) -> SearchAnalyticsRow:
    return SearchAnalyticsRow(
        keys={"query": query, "page": f"https://example.com/{query}"},
        clicks=clicks,
        impressions=impressions,
        ctr=ctr,
        position=position,
    )


def test_thresholds_select_rows_and_split_opportunity_kinds(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    rows = [
        _row("quick", impressions=300, position=8, ctr=0.01),
        _row("expand-position", impressions=250, position=25, ctr=0.01),
        _row("expand-ctr", impressions=200, position=8, ctr=0.05),
        _row("few", impressions=99, position=8, ctr=0.01),
        _row("too-high", impressions=500, position=4, ctr=0.01),
        _row("too-low", impressions=500, position=31, ctr=0.01),
    ]

    result = find_opportunities(rows, settings)

    assert [item.query for item in result] == ["quick", "expand-position", "expand-ctr"]
    assert [item.kind for item in result] == [
        "quick_win",
        "content_expansion",
        "content_expansion",
    ]
    assert "300" in result[0].reason
    assert "8.00" in result[0].reason
    assert "1.00%" in result[0].reason


def test_results_are_sorted_by_impressions_descending(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    rows = [
        _row("lower", impressions=120, position=10, ctr=0.01),
        _row("higher", impressions=900, position=10, ctr=0.01),
    ]

    assert [item.query for item in find_opportunities(rows, settings)] == ["higher", "lower"]


def test_changing_settings_thresholds_changes_results_without_magic_numbers(
    tmp_path: Path,
) -> None:
    row = _row("candidate", impressions=80, position=3, ctr=0.04)

    default_result = find_opportunities([row], Settings(data_dir=tmp_path / "default"))
    custom_result = find_opportunities(
        [row],
        Settings(
            data_dir=tmp_path / "custom",
            gsc_opportunity_min_impressions=50,
            gsc_opportunity_min_position=2,
            gsc_opportunity_max_position=10,
            gsc_opportunity_max_ctr=0.05,
        ),
    )

    assert default_result == []
    assert len(custom_result) == 1
    assert custom_result[0].kind == "quick_win"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("search_console_row_limit", 0),
        ("search_console_row_limit", 25001),
        ("search_console_daily_row_cap", 0),
        ("search_console_cache_ttl_seconds", 0),
        ("search_console_rate_limit_per_second", 0),
        ("gsc_opportunity_min_impressions", 0),
        ("gsc_opportunity_min_position", 0),
        ("gsc_opportunity_max_position", 0),
        ("gsc_opportunity_max_ctr", 0),
        ("gsc_opportunity_max_ctr", 1.01),
    ],
)
def test_search_console_settings_reject_invalid_values(field: str, value: float) -> None:
    with pytest.raises(InvalidConfigurationError):
        Settings.model_validate({field: value})


def test_position_window_must_be_ordered() -> None:
    with pytest.raises(InvalidConfigurationError, match="min_position"):
        Settings(gsc_opportunity_min_position=10, gsc_opportunity_max_position=10)
