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
    parse_timeline,
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


SPARSE_WIDGET_PAYLOADS = {
    "TIMESERIES": "multiline_sparse.json",
    "GEO_MAP": "comparedgeo_sparse.json",
    "RELATED_TOPICS": "relatedsearches_sparse.json",
    "RELATED_QUERIES": "relatedsearches_sparse.json",
}


def add_widget_routes(
    router: respx.MockRouter,
    explore_payload: str,
    *,
    failing_widget: str | None = None,
    timezone_minutes: int = -180,
    payloads: dict[str, str] | None = None,
) -> Iterator[respx.Route]:
    names = (
        {
            "TIMESERIES": "multiline_popular.json",
            "GEO_MAP": "comparedgeo_popular.json",
            "RELATED_TOPICS": "relatedsearches_popular.json",
            "RELATED_QUERIES": "relatedsearches_popular.json",
        }
        if payloads is None
        else payloads
    )
    responses = {widget: fixture(name) for widget, name in names.items()}
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


@pytest.mark.anyio
async def test_a_fetch_that_returned_no_data_at_all_is_not_cached(tmp_path: Path) -> None:
    """A widget outage must not be stored under the data's own six-hour TTL.

    A widget that fails is only a warning, so a moment in which Google refuses
    every one of them still returns a result -- an entirely empty one. Written
    to the cache it becomes the answer to that market for the whole TTL, and
    the run after the outage reads "no Trends data" from disk without ever
    asking Google again. An empty answer Google actually gave carries no
    warnings, which is what separates the two.
    """
    explore_payload = fixture("explore_popular.json")

    async def fetch(*, widgets_fail: bool) -> tuple[TrendsResult, list[str], int]:
        settings = Settings(
            data_dir=tmp_path,
            http_max_attempts=1,
            trends_pacing_seconds=0.001,
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
                # A cached answer issues no request at all, so the router must not
                # demand that every route be called: the call count is the assertion.
                with respx.mock(assert_all_called=False) as router:
                    router.get(WARMUP_URL).mock(return_value=httpx.Response(405))
                    explore = router.get(EXPLORE_URL).mock(
                        return_value=httpx.Response(200, text=explore_payload)
                    )
                    routes = list(add_widget_routes(router, explore_payload))
                    if widgets_fail:
                        for route in routes:
                            route.mock(return_value=httpx.Response(429, text="slow down"))
                    result = await provider.fetch(
                        ["недвижимость"], geo="RU", timeframe="today 12-m", hl="ru"
                    )
                    return result, provider.warnings, explore.call_count
        finally:
            engine.dispose()

    blocked, warnings, blocked_calls = await fetch(widgets_fail=True)
    assert blocked_calls == 1
    assert not blocked.timeline
    assert warnings, "the widget failures are what marks this result as an outage"

    recovered, _, recovered_calls = await fetch(widgets_fail=False)
    assert recovered_calls == 1, "the outage was served back from the cache"
    assert recovered.timeline, "the run after the outage got the empty stored answer"


@pytest.mark.anyio
@pytest.mark.parametrize("failing_widget", [None, "TIMESERIES"])
async def test_an_empty_answer_google_really_gave_is_still_cached(
    tmp_path: Path, failing_widget: str | None
) -> None:
    """Nothing to report is an answer, and answers are cached.

    The rule that keeps an outage out of the cache must not also keep out a
    keyword Google simply has no interest data for. These are the golden sparse
    replies, captured live: an empty `timelineData`, a `geoMapData` whose every
    row is marked `hasData: false`, and two empty ranked lists. The result is as
    empty as an outage, so emptiness cannot be the test -- what separates them
    is that a widget here answered.

    The second case is the one emptiness gets wrong on its own: the timeline
    widget really did fail, the other two answered and had nothing to report,
    and the run still holds an answer worth keeping.
    """
    explore_payload = fixture("explore_popular.json")
    settings = Settings(data_dir=tmp_path, http_max_attempts=1, trends_pacing_seconds=0.001)
    engine = open_database(settings)

    async def fetch() -> tuple[TrendsResult, list[str], int]:
        async with httpx.AsyncClient() as http_client:
            provider = GoogleTrendsProvider(
                settings=settings,
                client=http_client,
                cache=SqliteCache(engine, settings),
                rate_limiter=NoopRateLimiter(),
            )
            with respx.mock(assert_all_called=False) as router:
                router.get(WARMUP_URL).mock(return_value=httpx.Response(405))
                explore = router.get(EXPLORE_URL).mock(
                    return_value=httpx.Response(200, text=explore_payload)
                )
                list(
                    add_widget_routes(
                        router,
                        explore_payload,
                        failing_widget=failing_widget,
                        payloads=SPARSE_WIDGET_PAYLOADS,
                    )
                )
                result = await provider.fetch(
                    ["недвижимость"], geo="RU", timeframe="today 12-m", hl="ru"
                )
                return result, provider.warnings, explore.call_count

    try:
        first, warnings, first_calls = await fetch()
        _, _, second_calls = await fetch()
    finally:
        engine.dispose()

    assert first_calls == 1
    assert not first.timeline
    assert not first.geo_interest
    assert not first.related.top and not first.related.rising
    assert bool(warnings) == (failing_widget is not None)
    assert second_calls == 0, "an empty answer was refetched instead of being served from cache"


@pytest.mark.anyio
async def test_a_partial_result_that_still_carries_data_is_cached(tmp_path: Path) -> None:
    """One dead widget must not cost the other two a refetch every run.

    A failed widget leaves a warning behind, so the rule that keeps an outage
    out of the cache has to weigh the data as well: geography and related
    queries survived a dead timeline, and they are an answer. Refusing to store
    them would spend four requests against an unofficial, rate-limited endpoint
    on every run to retry one widget.
    """
    explore_payload = fixture("explore_popular.json")
    settings = Settings(data_dir=tmp_path, http_max_attempts=1, trends_pacing_seconds=0.001)
    engine = open_database(settings)

    async def fetch() -> tuple[TrendsResult, list[str], int]:
        async with httpx.AsyncClient() as http_client:
            provider = GoogleTrendsProvider(
                settings=settings,
                client=http_client,
                cache=SqliteCache(engine, settings),
                rate_limiter=NoopRateLimiter(),
            )
            with respx.mock(assert_all_called=False) as router:
                router.get(WARMUP_URL).mock(return_value=httpx.Response(405))
                explore = router.get(EXPLORE_URL).mock(
                    return_value=httpx.Response(200, text=explore_payload)
                )
                list(add_widget_routes(router, explore_payload, failing_widget="TIMESERIES"))
                result = await provider.fetch(
                    ["недвижимость"], geo="RU", timeframe="today 12-m", hl="ru"
                )
                return result, provider.warnings, explore.call_count

    try:
        first, warnings, first_calls = await fetch()
        second, _, second_calls = await fetch()
    finally:
        engine.dispose()

    assert first_calls == 1
    assert warnings, "the dead timeline is what makes this result a partial one"
    assert not first.timeline
    assert first.geo_interest and first.related.top, "the surviving widgets are the answer"
    assert second_calls == 0, "a partial result carrying data was refetched instead of cached"
    assert second.geo_interest == first.geo_interest


@pytest.mark.anyio
async def test_a_total_widget_outage_counts_against_the_circuit_breaker() -> None:
    """A run that got nothing has to count as a failure, however it failed.

    Explore surviving is not the same as the request succeeding: when every
    widget is refused, nothing came back at all. Resetting the counter there let
    a blocked endpoint be asked again on the very next run — and now that such a
    result is no longer written to the cache, nothing else was holding the next
    run back either.
    """
    explore_payload = fixture("explore_popular.json")
    settings = Settings(
        http_max_attempts=1,
        trends_circuit_breaker_failures=1,
        trends_pacing_seconds=0.001,
    )
    async with httpx.AsyncClient() as http_client:
        trends = make_client(settings, http_client)
        with respx.mock(assert_all_called=False) as router:
            router.get(WARMUP_URL).mock(return_value=httpx.Response(405))
            explore = router.get(EXPLORE_URL).mock(
                return_value=httpx.Response(200, text=explore_payload)
            )
            for route in add_widget_routes(router, explore_payload):
                route.mock(return_value=httpx.Response(429, text="slow down"))
            result = await trends.fetch(["one"], geo="RU", timeframe="today 12-m", hl="ru")
            assert not result.timeline
            assert trends.all_widgets_failed()
            with pytest.raises(ProviderUnavailableError):
                await trends.fetch(["one"], geo="RU", timeframe="today 12-m", hl="ru")

    assert explore.call_count == 1, "the breaker let a blocked endpoint be asked again"


@pytest.mark.anyio
async def test_a_widget_explore_never_offered_is_not_a_failure() -> None:
    """An absent widget is Google's answer, not a failure to reach it.

    `explore` lists the widgets it has. One it never listed is never requested,
    so counting it as refused would open the breaker on a keyword that was
    answered correctly and completely.
    """
    explore_payload = fixture("explore_popular.json")
    settings = Settings(http_max_attempts=1, trends_pacing_seconds=0.001)
    async with httpx.AsyncClient() as http_client:
        trends = make_client(settings, http_client)
        with respx.mock(assert_all_called=False) as router:
            router.get(WARMUP_URL).mock(return_value=httpx.Response(405))
            router.get(EXPLORE_URL).mock(return_value=httpx.Response(200, text=explore_payload))
            routes = list(add_widget_routes(router, explore_payload))
            for route in routes[1:]:
                route.mock(return_value=httpx.Response(429, text="slow down"))
            await trends.fetch(["one"], geo="RU", timeframe="today 12-m", hl="ru")

    assert not trends.all_widgets_failed(), "one widget answered, so the request did not fail"
    assert trends.widgets_attempted == len(routes)


@pytest.mark.anyio
async def test_an_explore_offering_nothing_to_fetch_is_an_answer(tmp_path: Path) -> None:
    """No widget asked for is not the same as every widget refused.

    `explore` lists what it has, and RELATED_TOPICS is deliberately not fetched:
    nothing reads topics. An `explore` that offers only that leaves no widget to
    request, so nothing can have been refused -- treating "none attempted" as a
    total failure would open the circuit breaker and reject the cache on a
    keyword Google answered correctly.
    """
    explore_payload = ")]}'\n" + json.dumps(
        {
            "widgets": [
                {
                    "id": "RELATED_TOPICS",
                    "request": {"restriction": {}},
                    "token": "topics-token",
                }
            ]
        }
    )
    settings = Settings(
        data_dir=tmp_path,
        http_max_attempts=1,
        trends_circuit_breaker_failures=1,
        trends_pacing_seconds=0.001,
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
            with respx.mock(assert_all_called=False) as router:
                router.get(WARMUP_URL).mock(return_value=httpx.Response(405))
                explore = router.get(EXPLORE_URL).mock(
                    return_value=httpx.Response(200, text=explore_payload)
                )
                widgetdata = router.get(url__startswith=WIDGETDATA_URL).mock(
                    return_value=httpx.Response(500, text="must not be requested")
                )
                await provider.fetch(["one"], geo="RU", timeframe="today 12-m", hl="ru")
                first_calls = explore.call_count
                await provider.fetch(["one"], geo="RU", timeframe="today 12-m", hl="ru")
                second_calls = explore.call_count
    finally:
        engine.dispose()

    assert widgetdata.call_count == 0, "a widget nothing reads must not be requested"
    assert first_calls == 1
    # Counting "none attempted" as a total failure would show up twice over: the
    # answer would not be cached, and with the threshold at one the breaker would
    # have refused the second fetch outright.
    assert second_calls == 1, "an answer with no widgets to fetch was not cached"


@pytest.mark.anyio
async def test_a_comparison_says_that_related_queries_came_per_keyword() -> None:
    """A widget split per keyword is not a widget Google does not have.

    `explore_compare.json` is a live two-keyword reply: Google returns
    `TIMESERIES` and `GEO_MAP` under their plain names, but related queries only
    as `RELATED_QUERIES_0` and `RELATED_QUERIES_1`. Looking the plain name up
    found nothing, and an absent widget is treated as Google's own answer -- so
    every comparison quietly reported no related queries at all and said
    nothing about it. They are still not merged: each is normalised inside its
    own widget, so one list of values across keywords would mean nothing.
    """
    explore_payload = fixture("explore_compare.json")
    settings = Settings(
        http_max_attempts=1,
        trends_circuit_breaker_failures=1,
        trends_pacing_seconds=0.001,
    )
    async with httpx.AsyncClient() as http_client:
        trends = make_client(settings, http_client)
        with respx.mock(assert_all_called=False) as router:
            router.get(WARMUP_URL).mock(return_value=httpx.Response(405))
            explore = router.get(EXPLORE_URL).mock(
                return_value=httpx.Response(200, text=explore_payload)
            )
            widgetdata = router.get(url__startswith=WIDGETDATA_URL).mock(
                return_value=httpx.Response(200, text=fixture("multiline_popular.json"))
            )
            result = await trends.fetch(
                ["недвижимость", "ипотека"], geo="RU", timeframe="today 12-m", hl="ru"
            )
            # With the threshold at one, a second comparison proves the split did
            # not count as a failed request: an outage would have shut the door.
            await trends.fetch(
                ["недвижимость", "ипотека"], geo="RU", timeframe="today 12-m", hl="ru"
            )
            assert explore.call_count == 2

    assert not result.related.top and not result.related.rising
    assert any("RELATED_QUERIES came back once per keyword" in w for w in trends.warnings), (
        trends.warnings
    )
    assert "RELATED_QUERIES_0" in trends.warnings[0] and "RELATED_QUERIES_1" in trends.warnings[0]
    # The plain-named widgets are still fetched, so this is a partial answer and
    # not an outage: the breaker must not count it and the cache may keep it.
    assert widgetdata.call_count == 4
    assert not trends.all_widgets_failed()


def test_the_golden_timeline_marks_its_unfinished_week() -> None:
    """The live capture ends mid-week, and the parser has to carry that through.

    `multiline_popular.json` was taken on a Wednesday: its last point covers
    30 August to 5 September and holds three days of seven. Dropped at parse
    time, that fragment is averaged into the latest quarter as a whole week.
    """
    timeline = parse_timeline(fixture("multiline_popular.json"))

    assert len(timeline) == 53
    assert timeline[-1].is_partial is True
    assert not any(point.is_partial for point in timeline[:-1])
