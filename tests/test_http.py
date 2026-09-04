from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import anyio
import httpx
import pytest
import respx

from google_keyword_ai.config import Settings
from google_keyword_ai.errors import ApiError, NetworkError, RateLimitError
from google_keyword_ai.http import build_client, request_with_retries

URL = "https://example.test/resource"


def test_build_client_uses_configured_headers_and_redirects(settings: Settings) -> None:
    async def inspect() -> None:
        async with build_client(settings) as client:
            assert client.headers["User-Agent"] == settings.http_user_agent
            assert client.headers["Accept-Language"] == settings.default_language
            assert client.follow_redirects is True

    anyio.run(inspect)


@pytest.mark.parametrize("status_code", [429, 500])
def test_retries_retryable_statuses(
    status_code: int, monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(anyio, "sleep", fake_sleep)
    settings = Settings(data_dir=data_dir, http_max_attempts=2, http_backoff_base_seconds=0.1)

    async def request() -> None:
        async with httpx.AsyncClient() as client:
            response = await request_with_retries(
                client, "GET", URL, params={"token": "secret"}, settings=settings
            )
        assert response.status_code == 200

    with respx.mock(assert_all_called=True) as router:
        route = router.get(URL).mock(side_effect=[httpx.Response(status_code), httpx.Response(200)])
        anyio.run(request)

    assert route.call_count == 2
    assert len(sleeps) == 1


def test_does_not_retry_status_400(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    async def fail_on_sleep(_delay: float) -> None:
        pytest.fail("non-retryable response must not sleep")

    monkeypatch.setattr(anyio, "sleep", fail_on_sleep)

    async def request() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ApiError) as caught:
                await request_with_retries(client, "GET", URL, params={}, settings=settings)
        assert caught.value.details == {"status_code": 400, "url": URL}

    with respx.mock(assert_all_called=True) as router:
        route = router.get(URL).mock(return_value=httpx.Response(400))
        anyio.run(request)

    assert route.call_count == 1


def test_retry_after_seconds_replaces_backoff(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(anyio, "sleep", fake_sleep)
    settings = Settings(data_dir=data_dir, http_max_attempts=2, http_backoff_base_seconds=99)

    async def request() -> None:
        async with httpx.AsyncClient() as client:
            await request_with_retries(client, "GET", URL, params={}, settings=settings)

    with respx.mock(assert_all_called=True) as router:
        router.get(URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "7"}),
                httpx.Response(200),
            ]
        )
        anyio.run(request)

    assert sleeps == [7.0]


@pytest.mark.parametrize(
    ("side_effect", "error_type", "status_code"),
    [
        (httpx.Response(429), RateLimitError, 429),
        (httpx.Response(503), ApiError, 503),
        (httpx.ConnectError("offline"), NetworkError, None),
    ],
)
def test_exhausted_attempts_raise_typed_error(
    side_effect: httpx.Response | Exception,
    error_type: type[Exception],
    status_code: int | None,
    monkeypatch: pytest.MonkeyPatch,
    data_dir: Path,
) -> None:
    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(anyio, "sleep", fake_sleep)
    settings = Settings(data_dir=data_dir, http_max_attempts=2)

    async def request() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(error_type) as caught:
                await request_with_retries(
                    client,
                    "GET",
                    f"{URL}?token=secret",
                    params={},
                    settings=settings,
                )
        error = caught.value
        assert isinstance(error, (ApiError, NetworkError, RateLimitError))
        assert error.details == {"status_code": status_code, "url": URL}

    with respx.mock(assert_all_called=True) as router:
        route = router.get(URL)
        if isinstance(side_effect, httpx.Response):
            route.mock(return_value=side_effect)
        else:
            route.mock(side_effect=side_effect)
        anyio.run(request)

    assert route.call_count == 2


def test_accept_language_follows_the_requested_market() -> None:
    """The header must not silently keep the configured default.

    Autocomplete carries the language in ``hl``, so a wrong header is invisible
    there, but Google Trends reads ``Accept-Language`` and would answer in the
    wrong language.
    """
    settings = Settings(default_language="en")

    async def check() -> None:
        async with build_client(settings, accept_language="ru") as client:
            assert client.headers["Accept-Language"] == "ru"
        async with build_client(settings) as client:
            assert client.headers["Accept-Language"] == "en"

    anyio.run(check)


def test_a_wait_longer_than_the_ceiling_is_reported_instead_of_slept(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    """A day-long `Retry-After` is a quota to come back for, not a retry.

    Obeying the header verbatim puts the whole run to sleep straight through
    the budget's runtime ceiling, with nothing printed and nothing to interrupt
    but the process. The advertised delay is more useful in the caller's hands
    than in a sleep it never asked for.
    """
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(anyio, "sleep", fake_sleep)
    settings = Settings(
        data_dir=data_dir,
        http_max_attempts=3,
        http_max_retry_after_seconds=60,
    )

    async def request() -> None:
        async with httpx.AsyncClient() as client:
            await request_with_retries(client, "GET", URL, params={}, settings=settings)

    with respx.mock(assert_all_called=True) as router:
        router.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "86400"}))
        with pytest.raises(RateLimitError) as raised:
            anyio.run(request)

    assert sleeps == [], "the run must not sleep for a day"
    assert raised.value.details["retry_after"] == 86400.0
    assert "86400s" in str(raised.value)


def test_a_wait_within_the_ceiling_is_still_honoured(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    """The ceiling must not turn an ordinary short backoff into a failure."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(anyio, "sleep", fake_sleep)
    settings = Settings(data_dir=data_dir, http_max_attempts=2, http_max_retry_after_seconds=60)

    async def request() -> None:
        async with httpx.AsyncClient() as client:
            await request_with_retries(client, "GET", URL, params={}, settings=settings)

    with respx.mock(assert_all_called=True) as router:
        router.get(URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "30"}),
                httpx.Response(200),
            ]
        )
        anyio.run(request)

    assert sleeps == [30.0]


def test_a_retry_after_date_is_honoured_like_a_delay(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    """`Retry-After` carries either a delay or a date, and both are instructions.

    RFC 9110 allows an HTTP-date, and Google's front ends send one. Reading only
    the numeric form dropped the header silently: the run backed off for a
    second instead of the day it was told to wait, and the ceiling that exists
    to turn a long wait into a reported quota never saw it either.
    """
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(anyio, "sleep", fake_sleep)
    settings = Settings(
        data_dir=data_dir,
        http_max_attempts=3,
        http_max_retry_after_seconds=60,
    )
    tomorrow = format_datetime(datetime.now(UTC) + timedelta(days=1), usegmt=True)

    async def request() -> None:
        async with httpx.AsyncClient() as client:
            await request_with_retries(client, "GET", URL, params={}, settings=settings)

    with respx.mock(assert_all_called=True) as router:
        router.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": tomorrow}))
        with pytest.raises(RateLimitError) as raised:
            anyio.run(request)

    assert sleeps == [], "the run must not sleep for a day, and must not ignore the date either"
    retry_after = raised.value.details["retry_after"]
    assert isinstance(retry_after, float)
    assert retry_after > 86000


def test_a_retry_after_date_already_past_waits_no_time(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    """A date that has already gone by says "now", not a negative delay.

    Clock skew and a slow hop both produce one, and the header still has to read
    as an instruction to retry rather than as a value to discard.
    """
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(anyio, "sleep", fake_sleep)
    settings = Settings(data_dir=data_dir, http_max_attempts=2, http_backoff_base_seconds=99)
    yesterday = format_datetime(datetime.now(UTC) - timedelta(days=1), usegmt=True)

    async def request() -> None:
        async with httpx.AsyncClient() as client:
            await request_with_retries(client, "GET", URL, params={}, settings=settings)

    with respx.mock(assert_all_called=True) as router:
        router.get(URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": yesterday}),
                httpx.Response(200),
            ]
        )
        anyio.run(request)

    assert sleeps == [0.0], "a past date is an immediate retry, not the backoff it replaced"
