import json
from pathlib import Path

import anyio
import httpx
import pytest
import respx

from google_keyword_ai.cache import SqliteCache
from google_keyword_ai.config import Settings
from google_keyword_ai.errors import ApiError
from google_keyword_ai.market import Market
from google_keyword_ai.providers.autocomplete import (
    FALLBACK_ENDPOINT,
    PRIMARY_ENDPOINT,
    AutocompleteProvider,
    parse_response,
)
from google_keyword_ai.ratelimit import AsyncRateLimiter
from google_keyword_ai.storage.engine import open_database

CHROME_PAYLOAD = [
    "аренда квартиры",
    ["аренда квартиры бангкок", "аренда квартиры паттайя"],
    ["", ""],
    [],
    {"google:suggestrelevance": [1252, 1251]},
]
FIREFOX_PAYLOAD = [
    "аренда квартиры",
    ["аренда квартиры москва", "аренда квартиры киев"],
    [],
    {"google:suggestsubtypes": [[512]]},
]


def _params(client_name: str) -> dict[str, str]:
    return {
        "client": client_name,
        "ie": "utf-8",
        "oe": "utf-8",
        "q": "аренда квартиры",
        "hl": "ru",
        "gl": "TH",
    }


def test_parse_chrome_format_with_relevances() -> None:
    suggestions, relevances = parse_response(json.dumps(CHROME_PAYLOAD, ensure_ascii=False))

    assert suggestions == CHROME_PAYLOAD[1]
    assert relevances == [1252, 1251]


def test_parse_firefox_format_without_relevances() -> None:
    suggestions, relevances = parse_response(json.dumps(FIREFOX_PAYLOAD, ensure_ascii=False))

    assert suggestions == FIREFOX_PAYLOAD[1]
    assert relevances == [None, None]


def test_short_relevance_list_is_padded() -> None:
    payload = ["q", ["one", "two", "three"], [], {}, {"google:suggestrelevance": [7]}]

    assert parse_response(json.dumps(payload))[1] == [7, None, None]


def test_invalid_json_raises_api_error() -> None:
    with pytest.raises(ApiError, match="invalid JSON"):
        parse_response("not-json")


def test_primary_failure_uses_fallback(data_dir: Path) -> None:
    settings = Settings(data_dir=data_dir, http_max_attempts=1)
    engine = open_database(settings)

    async def suggest() -> None:
        async with httpx.AsyncClient() as client:
            provider = AutocompleteProvider(
                settings=settings,
                client=client,
                cache=SqliteCache(engine, settings),
                rate_limiter=AsyncRateLimiter(1000),
            )
            result = await provider.suggest("аренда квартиры", Market.parse("ru", "TH"))
        assert [item.text for item in result] == FIREFOX_PAYLOAD[1]
        assert all(item.relevance is None for item in result)
        assert all(item.source == FALLBACK_ENDPOINT for item in result)

    try:
        with respx.mock(assert_all_called=True) as router:
            primary = router.get(PRIMARY_ENDPOINT, params=_params("chrome")).mock(
                return_value=httpx.Response(500)
            )
            fallback = router.get(FALLBACK_ENDPOINT, params=_params("firefox")).mock(
                return_value=httpx.Response(200, json=FIREFOX_PAYLOAD)
            )
            anyio.run(suggest)
        assert primary.call_count == fallback.call_count == 1
    finally:
        engine.dispose()


def test_second_identical_request_uses_cache_without_network(data_dir: Path) -> None:
    settings = Settings(data_dir=data_dir, http_max_attempts=1)
    engine = open_database(settings)

    async def suggest_twice() -> None:
        async with httpx.AsyncClient() as client:
            provider = AutocompleteProvider(
                settings=settings,
                client=client,
                cache=SqliteCache(engine, settings),
                rate_limiter=AsyncRateLimiter(1000),
            )
            first = await provider.suggest("аренда квартиры", Market.parse("ru", "TH"))
            second = await provider.suggest("аренда квартиры", Market.parse("ru", "TH"))
        assert first == second

    try:
        with respx.mock(assert_all_called=True) as router:
            route = router.get(PRIMARY_ENDPOINT, params=_params("chrome")).mock(
                return_value=httpx.Response(200, json=CHROME_PAYLOAD)
            )
            anyio.run(suggest_twice)
        assert route.call_count == 1
    finally:
        engine.dispose()
