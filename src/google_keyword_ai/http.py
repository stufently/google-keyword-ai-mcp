import random
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import anyio
import httpx

from google_keyword_ai.config import Settings
from google_keyword_ai.errors import ApiError, NetworkError, RateLimitError

RETRYABLE_STATUSES: tuple[int, ...] = (429, 500, 502, 503, 504)


def build_client(settings: Settings, *, accept_language: str | None = None) -> httpx.AsyncClient:
    """Build the shared async client.

    ``accept_language`` follows the market of the request rather than the
    configured default: a lookup for Russian issued from an English default
    would otherwise advertise the wrong language. Autocomplete takes the
    language from its own ``hl`` parameter, but Google Trends reads the header,
    so the two must not drift apart.
    """
    language = settings.default_language if accept_language is None else accept_language
    return httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers={
            "User-Agent": settings.http_user_agent,
            "Accept-Language": language,
        },
        follow_redirects=True,
    )


def _safe_url(url: str | httpx.URL) -> str:
    parsed = httpx.URL(url)
    return str(parsed.copy_with(query=None))


def _details(url: str | httpx.URL, status_code: int | None) -> dict[str, object]:
    return {"status_code": status_code, "url": _safe_url(url)}


def _retry_after(response: httpx.Response) -> float | None:
    """Read `Retry-After` in either form the standard allows.

    RFC 9110 lets the header carry a delay in seconds or an HTTP-date, and
    Google's front ends send both. Reading only the numeric form dropped the
    date silently: the run backed off for a second instead of the day it was
    told to wait, and the ceiling that turns a long wait into a reported quota
    never saw it. A date already in the past means "now", not a negative delay --
    clock skew and a slow hop both produce one.
    """
    if response.status_code not in {429, 503}:
        return None
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return _retry_after_date(value)
    return seconds if seconds >= 0 else None


def _retry_after_date(value: str) -> float | None:
    try:
        deadline = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if deadline.tzinfo is None:
        # RFC 9110 dates are GMT; a sender that omits the zone still means it.
        deadline = deadline.replace(tzinfo=UTC)
    return max(0.0, (deadline - datetime.now(UTC)).total_seconds())


async def request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, str],
    settings: Settings,
    retryable_statuses: Sequence[int] = RETRYABLE_STATUSES,
) -> httpx.Response:
    for attempt in range(1, settings.http_max_attempts + 1):
        try:
            response = await client.request(method, url, params=params)
        except httpx.TransportError as exc:
            if attempt == settings.http_max_attempts:
                raise NetworkError(
                    f"Network request failed after {attempt} attempts: {exc}",
                    _details(url, None),
                ) from exc
        else:
            if response.is_success:
                return response
            if response.status_code not in retryable_statuses:
                raise ApiError(
                    f"HTTP request failed with status {response.status_code}.",
                    _details(response.url, response.status_code),
                )
            if attempt == settings.http_max_attempts:
                error_type = RateLimitError if response.status_code == 429 else ApiError
                raise error_type(
                    f"HTTP request failed with status {response.status_code} "
                    f"after {attempt} attempts.",
                    _details(response.url, response.status_code),
                )
            asked_to_wait = _retry_after(response)
            if asked_to_wait is not None and asked_to_wait > settings.http_max_retry_after_seconds:
                # Obeying the header verbatim means a `Retry-After: 86400` puts
                # the whole run to sleep for a day, straight through the budget's
                # runtime ceiling and with nothing on screen. A wait this long is
                # a quota to come back for later, not a retry, so report it and
                # let the caller decide -- the number is in the details.
                raise RateLimitError(
                    f"Provider asked to wait {asked_to_wait:.0f}s, which is longer than the "
                    f"{settings.http_max_retry_after_seconds:.0f}s this run will wait.",
                    _details(response.url, response.status_code) | {"retry_after": asked_to_wait},
                )
            if asked_to_wait is not None:
                await anyio.sleep(asked_to_wait)
                continue

        delay = settings.http_backoff_base_seconds * 2 ** (attempt - 1)
        await anyio.sleep(delay + random.uniform(0, delay / 2))

    raise AssertionError("retry loop exited unexpectedly")
