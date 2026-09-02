import json
from collections.abc import Sequence

import httpx
from pydantic import BaseModel, ValidationError

from google_keyword_ai.cache import PARSER_VERSION, SqliteCache, build_cache_key
from google_keyword_ai.config import Settings
from google_keyword_ai.errors import ApiError, ProviderUnavailableError
from google_keyword_ai.providers.base import Provider, ProviderInfo
from google_keyword_ai.providers.trends.models import TrendsResult
from google_keyword_ai.providers.trends.official import OfficialTrendsAdapter
from google_keyword_ai.providers.trends.unofficial import EXPLORE_URL, UnofficialTrendsClient
from google_keyword_ai.ratelimit import AsyncRateLimiter


class _CachedResponse(BaseModel):
    result: TrendsResult
    warnings: list[str]


class GoogleTrendsProvider(Provider):
    def __init__(
        self,
        *,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        cache: SqliteCache | None = None,
        rate_limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self._settings = settings
        self._cache = cache
        self._official = OfficialTrendsAdapter()
        self._unofficial = (
            UnofficialTrendsClient(settings, client, rate_limiter)
            if client is not None and rate_limiter is not None
            else None
        )
        self.warnings: list[str] = []

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(name="trends", official=False, stability="unofficial")

    def is_available(self) -> bool:
        return self._settings.trends_enabled

    async def fetch(
        self,
        keywords: Sequence[str],
        *,
        geo: str,
        timeframe: str,
        hl: str,
    ) -> TrendsResult:
        if not self.is_available():
            raise ProviderUnavailableError("Google Trends is disabled by configuration.")
        if self._cache is None:
            raise ProviderUnavailableError("Google Trends cache is not configured.")

        keyword_list = list(keywords)
        params = {
            "keywords": json.dumps(keyword_list, ensure_ascii=False, separators=(",", ":")),
            "geo": geo,
            "timeframe": timeframe,
            "hl": hl,
        }
        cache_key = build_cache_key(
            self.info.name,
            EXPLORE_URL,
            params,
            account_scope="",
            parser_version=PARSER_VERSION,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            try:
                decoded = _CachedResponse.model_validate_json(cached)
            except ValidationError as exc:
                raise ApiError("Google Trends cache entry is invalid.") from exc
            self.warnings = list(decoded.warnings)
            return decoded.result

        if self._official.is_available():
            result = await self._official.fetch(
                keyword_list,
                geo=geo,
                timeframe=timeframe,
                hl=hl,
            )
            self.warnings = []
        else:
            if self._unofficial is None:
                raise ProviderUnavailableError("Unofficial Google Trends client is not configured.")
            result = await self._unofficial.fetch(
                keyword_list,
                geo=geo,
                timeframe=timeframe,
                hl=hl,
            )
            self.warnings = list(self._unofficial.warnings)

        payload = _CachedResponse(result=result, warnings=self.warnings).model_dump_json().encode()
        self._cache.set(
            cache_key,
            provider=self.info.name,
            endpoint=EXPLORE_URL,
            account_scope="",
            parser_version=PARSER_VERSION,
            payload=payload,
            ttl_seconds=self._settings.trends_cache_ttl_seconds,
        )
        return result
