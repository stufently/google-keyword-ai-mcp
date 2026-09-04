import json
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from google_keyword_ai.cache import PARSER_VERSION, SqliteCache, build_cache_key
from google_keyword_ai.config import Settings
from google_keyword_ai.errors import ApiError, GkaiError, RateLimitError
from google_keyword_ai.http import request_with_retries
from google_keyword_ai.market import Market
from google_keyword_ai.providers.base import Provider, ProviderInfo
from google_keyword_ai.ratelimit import AsyncRateLimiter

PRIMARY_ENDPOINT = "https://www.google.com/complete/search"
FALLBACK_ENDPOINT = "https://suggestqueries.google.com/complete/search"


class Suggestion(BaseModel):
    text: str
    relevance: int | None
    source: str
    retrieved_at: datetime


_SUGGESTIONS_ADAPTER = TypeAdapter(list[Suggestion])


def parse_response(payload: str) -> tuple[list[str], list[int | None]]:
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError("Autocomplete returned invalid JSON.") from exc

    if (
        not isinstance(decoded, list)
        or len(decoded) < 2
        or not isinstance(decoded[1], list)
        or not all(isinstance(item, str) for item in decoded[1])
    ):
        raise ApiError("Autocomplete returned an unexpected response shape.")

    suggestions: list[str] = decoded[1]
    raw_relevances: object = None
    if decoded and isinstance(decoded[-1], dict):
        raw_relevances = decoded[-1].get("google:suggestrelevance")

    relevance_values = raw_relevances if isinstance(raw_relevances, list) else []
    relevances: list[int | None] = []
    for index in range(len(suggestions)):
        value = relevance_values[index] if index < len(relevance_values) else None
        relevances.append(value if isinstance(value, int) and not isinstance(value, bool) else None)
    return suggestions, relevances


class AutocompleteProvider(Provider):
    def __init__(
        self,
        *,
        settings: Settings,
        client: httpx.AsyncClient,
        cache: SqliteCache,
        rate_limiter: AsyncRateLimiter,
    ) -> None:
        self._settings = settings
        self._client = client
        self._cache = cache
        self._rate_limiter = rate_limiter

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(name="autocomplete", official=False, stability="unofficial")

    def is_available(self) -> bool:
        return True

    async def suggest(
        self, query: str, market: Market, *, limit: int | None = None
    ) -> list[Suggestion]:
        try:
            suggestions = await self._suggest_from_endpoint(
                PRIMARY_ENDPOINT, "chrome", query, market
            )
        except RateLimitError:
            # Not something a second host answers. Google asked this run to slow
            # down; catching it with every other failure discarded the message,
            # the status and the advertised delay, and fired a fresh request at
            # the other endpoint -- turning a throttled fan-out of 400 requests
            # into 1600, all of them while being throttled, and reporting the
            # run complete because the fallback happened to answer.
            raise
        except GkaiError:
            suggestions = await self._suggest_from_endpoint(
                FALLBACK_ENDPOINT, "firefox", query, market
            )
        return suggestions if limit is None else suggestions[:limit]

    async def _suggest_from_endpoint(
        self, endpoint: str, client_name: str, query: str, market: Market
    ) -> list[Suggestion]:
        params = {
            "client": client_name,
            "ie": "utf-8",
            "oe": "utf-8",
            "q": query,
            **market.autocomplete_params(),
        }
        cache_key = build_cache_key(
            self.info.name,
            endpoint,
            params,
            account_scope="",
            parser_version=PARSER_VERSION,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            try:
                return _SUGGESTIONS_ADAPTER.validate_json(cached)
            except ValidationError as exc:
                raise ApiError("Autocomplete cache entry is invalid.") from exc

        await self._rate_limiter.acquire()
        response = await request_with_retries(
            self._client,
            "GET",
            endpoint,
            params=params,
            settings=self._settings,
        )
        texts, relevances = parse_response(response.text)
        retrieved_at = datetime.now(UTC)
        suggestions = [
            Suggestion(
                text=text,
                relevance=relevance,
                source=endpoint,
                retrieved_at=retrieved_at,
            )
            for text, relevance in zip(texts, relevances, strict=True)
        ]
        self._cache.set(
            cache_key,
            provider=self.info.name,
            endpoint=endpoint,
            account_scope="",
            parser_version=PARSER_VERSION,
            payload=_SUGGESTIONS_ADAPTER.dump_json(suggestions),
            ttl_seconds=self._settings.autocomplete_cache_ttl_seconds,
        )
        return suggestions
