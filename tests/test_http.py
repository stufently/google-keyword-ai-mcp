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
