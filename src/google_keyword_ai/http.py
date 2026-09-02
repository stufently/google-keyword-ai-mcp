import random
from collections.abc import Mapping, Sequence

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
    if response.status_code not in {429, 503}:
        return None
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


async def request_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, str],
    settings: Settings,
    retryable_statuses: Sequence[int] = RETRYABLE_STATUSES,
) -> httpx.Response:
    last_transport_error: httpx.TransportError | None = None

    for attempt in range(1, settings.http_max_attempts + 1):
        try:
            response = await client.request(method, url, params=params)
            last_transport_error = None
        except httpx.TransportError as exc:
            last_transport_error = exc
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

        delay = settings.http_backoff_base_seconds * 2 ** (attempt - 1)
        if last_transport_error is None:
            retry_after = _retry_after(response)
            if retry_after is not None:
                await anyio.sleep(retry_after)
                continue
        await anyio.sleep(delay + random.uniform(0, delay / 2))

    raise AssertionError("retry loop exited unexpectedly")
