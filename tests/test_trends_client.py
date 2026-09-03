import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import anyio
import httpx
import pytest
import respx

from google_keyword_ai.cache import SqliteCache
from google_keyword_ai.config import Settings
from google_keyword_ai.errors import ApiError, ProviderUnavailableError
from google_keyword_ai.providers.trends.models import TrendsResult
from google_keyword_ai.providers.trends.provider import GoogleTrendsProvider
from google_keyword_ai.providers.trends.unofficial import (
    CONSUMED_WIDGETS,
    EXPLORE_URL,
    WARMUP_URL,
    WIDGET_PATHS,
    WIDGETDATA_URL,
    UnofficialTrendsClient,
    strip_prefix,
)
from google_keyword_ai.ratelimit import AsyncRateLimiter
from google_keyword_ai.storage.engine import open_database

FIXTURES = Path(__file__).parent / "fixtures" / "trends"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def explore_widgets(payload: str) -> list[dict[str, object]]:
    decoded = json.loads(strip_prefix(payload))
    widgets = decoded["widgets"]
    assert isinstance(widgets, list)
    return cast(list[dict[str, object]], widgets)


class NoopRateLimiter(AsyncRateLimiter):
    def __init__(self) -> None:
        super().__init__(1.0)

    async def acquire(self) -> None:
        return None


def add_widget_routes(
    router: respx.MockRouter,
    explore_payload: str,
    *,
    failing_widget: str | None = None,
    timezone_minutes: int = -180,
) -> Iterator[respx.Route]:
    responses = {
        "TIMESERIES": fixture("multiline_popular.json"),
        "GEO_MAP": fixture("comparedgeo_popular.json"),
        "RELATED_TOPICS": fixture("relatedsearches_popular.json"),
        "RELATED_QUERIES": fixture("relatedsearches_popular.json"),
    }
    for widget in explore_widgets(explore_payload):
        widget_id = cast(str, widget["id"])
        if widget_id not in CONSUMED_WIDGETS:
            # Registering a route for a widget the client must not fetch would
            # turn assert_all_called into a demand that it be fetched.
            continue
        request = cast(dict[str, object], widget["request"])
        token = cast(str, widget["token"])
        route = router.get(
            f"{WIDGETDATA_URL}/{WIDGET_PATHS[widget_id]}",
            params={
                "hl": "ru",
                "tz": str(timezone_minutes),
                "req": json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                "token": token,
            },
        )
        if widget_id == failing_widget:
            route.mock(return_value=httpx.Response(500, text="failed"))
        else:
            route.mock(return_value=httpx.Response(200, text=responses[widget_id]))
        yield route


def make_client(settings: Settings, client: httpx.AsyncClient) -> UnofficialTrendsClient:
    return UnofficialTrendsClient(settings, client, NoopRateLimiter())


@pytest.mark.anyio
async def test_warmup_405_is_success_and_runs_once() -> None:
    settings = Settings(http_max_attempts=1)
    async with httpx.AsyncClient() as http_client:
        trends = make_client(settings, http_client)
        with respx.mock(assert_all_called=True) as router:
            warmup = router.get(WARMUP_URL).mock(return_value=httpx.Response(405))
            await trends.warm_up()
            await trends.warm_up()

    assert warmup.call_count == 1


@pytest.mark.anyio
async def test_widget_calls_observe_configured_pacing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(http_max_attempts=1, trends_pacing_seconds=0.25)
    explore_payload = fixture("explore_popular.json")
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(anyio, "sleep", record_sleep)
    async with httpx.AsyncClient() as http_client:
        trends = make_client(settings, http_client)
        with respx.mock(assert_all_called=True) as router:
            router.get(WARMUP_URL).mock(return_value=httpx.Response(405))
            router.get(EXPLORE_URL).mock(return_value=httpx.Response(200, text=explore_payload))
            list(add_widget_routes(router, explore_payload))
            await trends.fetch(["недвижимость"], geo="RU", timeframe="today 12-m", hl="ru")

    # Three widgets are consumed, so there are two gaps between them.
    assert sleeps == [0.25, 0.25]


@pytest.mark.anyio
async def test_missing_widget_is_not_requested() -> None:
    settings = Settings(http_max_attempts=1, trends_pacing_seconds=0.001)
    raw = json.loads(strip_prefix(fixture("explore_popular.json")))
    raw["widgets"] = [widget for widget in raw["widgets"] if widget["id"] != "GEO_MAP"]
    explore_payload = ")]}'\n" + json.dumps(raw, ensure_ascii=False)

    async with httpx.AsyncClient() as http_client:
        trends = make_client(settings, http_client)
        with respx.mock(assert_all_called=True) as router:
            router.get(WARMUP_URL).mock(return_value=httpx.Response(405))
            router.get(EXPLORE_URL).mock(return_value=httpx.Response(200, text=explore_payload))
            list(add_widget_routes(router, explore_payload))
            result = await trends.fetch(["недвижимость"], geo="RU", timeframe="today 12-m", hl="ru")

    assert result.geo_interest == []
    assert not any("GEO_MAP" in warning for warning in trends.warnings)


@pytest.mark.anyio
async def test_widget_failure_keeps_other_widget_data() -> None:
    settings = Settings(http_max_attempts=1, trends_pacing_seconds=0.001)
    explore_payload = fixture("explore_popular.json")

    async with httpx.AsyncClient() as http_client:
        trends = make_client(settings, http_client)
        with respx.mock(assert_all_called=True) as router:
            router.get(WARMUP_URL).mock(return_value=httpx.Response(405))
            router.get(EXPLORE_URL).mock(return_value=httpx.Response(200, text=explore_payload))
            list(add_widget_routes(router, explore_payload, failing_widget="GEO_MAP"))
            result = await trends.fetch(["недвижимость"], geo="RU", timeframe="today 12-m", hl="ru")

    assert len(result.timeline) == 53
    assert result.geo_interest == []
    assert len(result.related.top) == 25
    assert any("GEO_MAP" in warning for warning in trends.warnings)


@pytest.mark.anyio
async def test_circuit_breaker_stops_network_after_consecutive_failures() -> None:
    settings = Settings(
        http_max_attempts=1,
        trends_circuit_breaker_failures=2,
        trends_pacing_seconds=0.001,
    )
    async with httpx.AsyncClient() as http_client:
        trends = make_client(settings, http_client)
        with respx.mock(assert_all_called=True) as router:
            warmup = router.get(WARMUP_URL).mock(return_value=httpx.Response(405))
            explore = router.get(EXPLORE_URL).mock(return_value=httpx.Response(500))
            with pytest.raises(ApiError):
                await trends.fetch(["one"], geo="US", timeframe="today 12-m", hl="en")
            with pytest.raises(ApiError):
                await trends.fetch(["one"], geo="US", timeframe="today 12-m", hl="en")
            with pytest.raises(ProviderUnavailableError):
                await trends.fetch(["one"], geo="US", timeframe="today 12-m", hl="en")

    assert warmup.call_count == 1
    assert explore.call_count == 2


@pytest.mark.anyio
async def test_provider_second_request_comes_from_cache(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        http_max_attempts=1,
        trends_pacing_seconds=0.001,
    )
    engine = open_database(settings)
    explore_payload = fixture("explore_popular.json")
    try:
        async with httpx.AsyncClient() as http_client:
            provider = GoogleTrendsProvider(
                settings=settings,
                client=http_client,
                cache=SqliteCache(engine, settings),
                rate_limiter=NoopRateLimiter(),
            )
            with respx.mock(assert_all_called=True) as router:
                router.get(WARMUP_URL).mock(return_value=httpx.Response(405))
                router.get(EXPLORE_URL).mock(return_value=httpx.Response(200, text=explore_payload))
                list(add_widget_routes(router, explore_payload))
                first = await provider.fetch(
                    ["недвижимость"], geo="RU", timeframe="today 12-m", hl="ru"
                )
            with respx.mock(assert_all_called=True):
                second = await provider.fetch(
                    ["недвижимость"], geo="RU", timeframe="today 12-m", hl="ru"
                )
    finally:
        engine.dispose()

    assert second == first


@pytest.mark.anyio
async def test_the_timezone_offset_is_part_of_the_cache_key(tmp_path: Path) -> None:
    """A timeline cut on one day boundary must not answer for another.

    The offset travels to Trends as `tz` and decides where each bucket of the
    timeline begins. Outside the key, the first run's answer is served to a run
    under a different offset, which then reads points aligned to a zone it
    never asked about -- and nothing in the reply says so.
    """
    explore_payload = fixture("explore_popular.json")

    async def fetch_under(offset: int) -> tuple[TrendsResult, int]:
        settings = Settings(
            data_dir=tmp_path,
            http_max_attempts=1,
            trends_pacing_seconds=0.001,
            trends_timezone_minutes=offset,
        )
        engine = open_database(settings)
        try:
            async with httpx.AsyncClient() as http_client:
                provider = GoogleTrendsProvider(
                    settings=settings,
                    client=http_client,
                    cache=SqliteCache(engine, settings),
                    rate_limiter=NoopRateLimiter(),
                )
                with respx.mock() as router:
                    router.get(WARMUP_URL).mock(return_value=httpx.Response(405))
                    explore = router.get(EXPLORE_URL).mock(
                        return_value=httpx.Response(200, text=explore_payload)
                    )
                    list(add_widget_routes(router, explore_payload, timezone_minutes=offset))
                    result = await provider.fetch(
                        ["недвижимость"], geo="RU", timeframe="today 12-m", hl="ru"
                    )
                    return result, explore.call_count
        finally:
            engine.dispose()

    first, first_calls = await fetch_under(-180)
    second, second_calls = await fetch_under(0)

    assert first_calls == 1
    assert second_calls == 1, "the second offset was answered from the first offset's cache"
    # Both runs read the same fixture, so a second run that really went out
    # returns the same timeline: the call count is what separates a fresh
    # answer from the first offset's stored one.
    assert second.timeline == first.timeline


@pytest.mark.anyio
async def test_related_topics_widget_is_never_requested() -> None:
    """A widget nothing reads must not be fetched.

    Google Trends is an unofficial, rate-limited endpoint and every extra call
    also costs a pacing pause. RELATED_TOPICS has a path in WIDGET_PATHS but no
    parser, so requesting it would spend a call to throw the answer away.
    """
    explore_payload = fixture("explore_popular.json")
    settings = Settings(http_max_attempts=1, trends_pacing_seconds=0.001)
    async with httpx.AsyncClient() as http_client:
        trends = make_client(settings, http_client)
        with respx.mock(assert_all_called=True) as router:
            router.get(WARMUP_URL).mock(return_value=httpx.Response(405))
            router.get(EXPLORE_URL).mock(return_value=httpx.Response(200, text=explore_payload))
            list(add_widget_routes(router, explore_payload))
            await trends.fetch(["недвижимость"], geo="RU", timeframe="today 12-m", hl="ru")

            requested_tokens = {request.url.params.get("token") for request, _ in router.calls}

    topics_widget = next(
        widget for widget in explore_widgets(explore_payload) if widget["id"] == "RELATED_TOPICS"
    )
    assert cast(str, topics_widget["token"]) not in requested_tokens
