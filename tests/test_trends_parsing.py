from pathlib import Path

import pytest

from google_keyword_ai.errors import ApiError
from google_keyword_ai.providers.trends.unofficial import (
    parse_explore,
    parse_geo,
    parse_related,
    parse_timeline,
    strip_prefix,
)

FIXTURES = Path(__file__).parent / "fixtures" / "trends"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (')]}\'\n{"value": 1}', '{"value": 1}'),
        (')]}\' ,\n{"value": 2}', '{"value": 2}'),
        ('  )]}\',\n{"value": 3}', '{"value": 3}'),
    ],
)
def test_strip_prefix_accepts_explore_and_widget_prefixes(payload: str, expected: str) -> None:
    assert strip_prefix(payload) == expected


def test_strip_prefix_rejects_payload_without_json_object() -> None:
    with pytest.raises(ApiError):
        strip_prefix(")]}' not-json")


def test_parse_popular_explore_has_all_four_widgets() -> None:
    widgets = parse_explore(fixture("explore_popular.json"))

    assert set(widgets) == {
        "TIMESERIES",
        "GEO_MAP",
        "RELATED_TOPICS",
        "RELATED_QUERIES",
    }


def test_parse_popular_timeline_has_expected_points() -> None:
    timeline = parse_timeline(fixture("multiline_popular.json"))

    assert len(timeline) == 53
    assert timeline[0].timestamp.isoformat() == "2025-08-31T00:00:00+00:00"
    assert timeline[0].values == [98]


def test_parse_popular_geo_has_expected_rows() -> None:
    geo = parse_geo(fixture("comparedgeo_popular.json"))

    assert len(geo) == 83
    assert geo[0].geo_code == "RU-SAK"
    assert geo[0].values == [100]


def test_parse_popular_related_preserves_top_and_rising() -> None:
    related = parse_related(fixture("relatedsearches_popular.json"))

    assert len(related.top) == 25
    assert len(related.rising) == 15
    assert related.top[0].has_data is True
    assert related.rising[0].has_data is None


def test_parse_sparse_explore_still_has_widgets() -> None:
    assert len(parse_explore(fixture("explore_sparse.json"))) == 4


def test_parse_sparse_timeline_is_empty() -> None:
    assert parse_timeline(fixture("multiline_sparse.json")) == []


def test_parse_sparse_related_lists_are_empty() -> None:
    related = parse_related(fixture("relatedsearches_sparse.json"))

    assert related.top == []
    assert related.rising == []


def test_parse_sparse_geo_without_data_is_empty() -> None:
    assert parse_geo(fixture("comparedgeo_sparse.json")) == []


@pytest.mark.parametrize("parser", [parse_timeline, parse_geo, parse_related])
def test_widget_parser_requires_default_node(parser: object) -> None:
    assert callable(parser)
    with pytest.raises(ApiError):
        parser(")]}'\n{}")
