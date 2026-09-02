import json
from collections.abc import Callable, Iterable, Sequence
from enum import Enum
from functools import partial
from typing import Any, cast

import anyio
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from google_keyword_ai.cache import PARSER_VERSION, SqliteCache, build_cache_key
from google_keyword_ai.config import Settings
from google_keyword_ai.errors import (
    ApiError,
    AuthenticationError,
    GkaiError,
    InvalidConfigurationError,
    ProviderUnavailableError,
    RateLimitError,
)
from google_keyword_ai.market import Market
from google_keyword_ai.providers.base import Provider, ProviderInfo
from google_keyword_ai.ratelimit import InterProcessRateLimiter

IDEAS_ENDPOINT = "generate_keyword_ideas"
HISTORICAL_ENDPOINT = "generate_keyword_historical_metrics"
_NETWORK = "GOOGLE_SEARCH_AND_PARTNERS"


class MonthlyVolume(BaseModel):
    year: int
    month: str
    monthly_searches: int


class KeywordMetrics(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    avg_monthly_searches: int | None = None
    monthly_search_volumes: list[MonthlyVolume] = Field(default_factory=list)
    competition: str | None = Field(default=None, serialization_alias="ads_competition")
    competition_index: int | None = None
    low_top_of_page_bid: float | None = None
    high_top_of_page_bid: float | None = None
    average_cpc: float | None = None
    currency: str | None = None


class KeywordIdea(BaseModel):
    text: str
    metrics: KeywordMetrics


class AdsSeed(BaseModel):
    model_config = ConfigDict(frozen=True)

    keywords: list[str] = Field(default_factory=list)
    url: str | None = None
    site: str | None = None

    def mode(self) -> str:
        has_keywords = bool(self.keywords)
        has_url = bool(self.url)
        has_site = bool(self.site)
        if has_site and (has_keywords or has_url):
            raise InvalidConfigurationError(
                "Google Ads site seed cannot be combined with keywords or a URL."
            )
        if has_site:
            return "site_seed"
        if has_keywords and has_url:
            return "keyword_and_url_seed"
        if has_keywords:
            return "keyword_seed"
        if has_url:
            return "url_seed"
        raise InvalidConfigurationError("Google Ads seed must not be empty.")


_IDEAS_ADAPTER = TypeAdapter(list[KeywordIdea])


def _library_exception_types() -> tuple[type[BaseException], ...]:
    from google.ads.googleads.errors import GoogleAdsException  # type: ignore[import-untyped]
    from google.api_core.exceptions import GoogleAPIError

    return (GoogleAdsException, GoogleAPIError)


def _status_name(exc: BaseException) -> str:
    from google.ads.googleads.errors import GoogleAdsException

    error: object = exc.error if isinstance(exc, GoogleAdsException) else exc
    code_method = getattr(error, "code", None)
    code = code_method() if callable(code_method) else code_method
    if isinstance(code, Enum):
        return code.name
    return str(code).rsplit(".", maxsplit=1)[-1].upper()


def _translate_library_error(exc: BaseException) -> GkaiError:
    status = _status_name(exc)
    if status in {"RESOURCE_EXHAUSTED", "TOO_MANY_REQUESTS"}:
        return RateLimitError("Google Ads quota is exhausted.")
    if status in {"UNAUTHENTICATED", "UNAUTHORIZED", "PERMISSION_DENIED", "FORBIDDEN"}:
        return AuthenticationError("Google Ads authentication or authorization failed.")
    return ApiError(f"Google Ads API request failed ({status}).")


def _optional_value(message: object, field: str) -> object | None:
    protobuf = getattr(message, "_pb", None)
    if protobuf is not None:
        try:
            if not protobuf.HasField(field):
                return None
        except ValueError:
            pass
    return getattr(message, field, None)


def _required_value(message: object, field: str) -> object:
    return getattr(message, field)


def _enum_name(value: object | None) -> str | None:
    if value is None or value == 0:
        return None
    name = getattr(value, "name", None)
    return str(name) if name is not None else str(value)


def _optional_int(message: object, field: str) -> int | None:
    value = _optional_value(message, field)
    return None if value is None else int(cast(int, value))


def _micros(message: object, field: str) -> float | None:
    value = _optional_int(message, field)
    return None if value is None else value / 1_000_000


def _parse_metrics(metrics: object) -> KeywordMetrics:
    raw_months = cast(Iterable[object], getattr(metrics, "monthly_search_volumes", ()))
    monthly_volumes = [
        MonthlyVolume(
            year=int(cast(int, _required_value(volume, "year"))),
            month=_enum_name(getattr(volume, "month", None)) or "UNSPECIFIED",
            monthly_searches=int(cast(int, _required_value(volume, "monthly_searches"))),
        )
        for volume in raw_months
    ]
    return KeywordMetrics(
        avg_monthly_searches=_optional_int(metrics, "avg_monthly_searches"),
        monthly_search_volumes=monthly_volumes,
        competition=_enum_name(getattr(metrics, "competition", None)),
        competition_index=_optional_int(metrics, "competition_index"),
        low_top_of_page_bid=_micros(metrics, "low_top_of_page_bid_micros"),
        high_top_of_page_bid=_micros(metrics, "high_top_of_page_bid_micros"),
        average_cpc=_micros(metrics, "average_cpc_micros"),
        currency=None,
    )


def _parse_ideas(response: object, *, historical: bool) -> list[KeywordIdea]:
    rows = getattr(response, "results", ()) if historical else response
    metrics_field = "keyword_metrics" if historical else "keyword_idea_metrics"
    return [
        KeywordIdea(
            text=str(_required_value(row, "text")),
            metrics=_parse_metrics(_required_value(row, metrics_field)),
        )
        for row in cast(Iterable[object], rows)
    ]


class GoogleAdsProvider(Provider):
    def __init__(
        self,
        *,
        settings: Settings,
        cache: SqliteCache | None,
        rate_limiter: InterProcessRateLimiter | None,
        service_factory: Callable[[], object] | None = None,
    ) -> None:
        self._settings = settings
        self._cache = cache
        self._rate_limiter = rate_limiter
        self._service_factory = service_factory

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(name="google_ads", official=True, stability="stable")

    def is_available(self) -> bool:
        secrets = (
            self._settings.google_ads_developer_token,
            self._settings.google_ads_client_id,
            self._settings.google_ads_client_secret,
            self._settings.google_ads_refresh_token,
        )
        return bool((self._settings.google_ads_customer_id or "").strip()) and all(
            secret is not None and bool(secret.get_secret_value().strip()) for secret in secrets
        )

    def build_service(self) -> object:
        if self._service_factory is not None:
            return self._service_factory()

        from google.ads.googleads.client import GoogleAdsClient  # type: ignore[import-untyped]

        developer_token = self._settings.google_ads_developer_token
        client_id = self._settings.google_ads_client_id
        client_secret = self._settings.google_ads_client_secret
        refresh_token = self._settings.google_ads_refresh_token
        if (
            developer_token is None
            or client_id is None
            or client_secret is None
            or refresh_token is None
        ):
            raise ProviderUnavailableError("Google Ads credentials are incomplete.")
        config: dict[str, str | bool] = {
            "developer_token": developer_token.get_secret_value(),
            "client_id": client_id.get_secret_value(),
            "client_secret": client_secret.get_secret_value(),
            "refresh_token": refresh_token.get_secret_value(),
            "use_proto_plus": True,
        }
        if self._settings.google_ads_login_customer_id:
            config["login_customer_id"] = self._settings.google_ads_login_customer_id
        client = GoogleAdsClient.load_from_dict(
            config,
            version=self._settings.google_ads_api_version,
        )
        return client.get_service("KeywordPlanIdeaService")

    def _require_available(self) -> str:
        if not self.is_available():
            raise ProviderUnavailableError("Google Ads credentials are missing or incomplete.")
        customer_id = self._settings.google_ads_customer_id
        assert customer_id is not None
        return customer_id

    def _cache_key(self, endpoint: str, params: dict[str, str], customer_id: str) -> str:
        return build_cache_key(
            self.info.name,
            endpoint,
            params,
            account_scope=customer_id,
            parser_version=PARSER_VERSION,
        )

    def _cached(self, key: str) -> list[KeywordIdea] | None:
        if self._cache is None:
            raise ProviderUnavailableError("Google Ads cache is not configured.")
        payload = self._cache.get(key)
        if payload is None:
            return None
        try:
            return _IDEAS_ADAPTER.validate_json(payload)
        except ValidationError as exc:
            raise ApiError("Google Ads cache entry is invalid.") from exc

    def _store(
        self,
        key: str,
        endpoint: str,
        customer_id: str,
        ideas: list[KeywordIdea],
        ttl_seconds: int,
    ) -> None:
        if self._cache is None:
            raise ProviderUnavailableError("Google Ads cache is not configured.")
        self._cache.set(
            key,
            provider=self.info.name,
            endpoint=endpoint,
            account_scope=customer_id,
            parser_version=PARSER_VERSION,
            payload=_IDEAS_ADAPTER.dump_json(ideas, by_alias=False),
            ttl_seconds=ttl_seconds,
        )

    @staticmethod
    def _invoke_and_parse(
        service: object,
        method_name: str,
        request: dict[str, Any],
        *,
        historical: bool,
    ) -> list[KeywordIdea]:
        method = cast(Callable[..., object], getattr(service, method_name))
        response = method(request=request)
        return _parse_ideas(response, historical=historical)

    @staticmethod
    def _invoke(service: object, method_name: str, request: dict[str, Any]) -> object:
        method = cast(Callable[..., object], getattr(service, method_name))
        return method(request=request)

    async def _call_ideas(
        self,
        service: object,
        request: dict[str, Any],
    ) -> list[KeywordIdea]:
        """Walk the pager one page at a time, throttling between pages.

        ``generate_keyword_ideas`` returns a pager, not a plain response: every
        page after the first is another RPC, issued lazily while the results are
        iterated. Consuming it inside a single worker thread would fire those
        calls back to back and blow through the one-request-per-second limit
        that the first page was carefully throttled for.
        """
        if self._rate_limiter is None:
            raise ProviderUnavailableError("Google Ads rate limiter is not configured.")

        try:
            pager = await anyio.to_thread.run_sync(
                partial(self._invoke, service, IDEAS_ENDPOINT, request)
            )
            pages = iter(cast(Iterable[object], getattr(pager, "pages", [pager])))
            ideas: list[KeywordIdea] = []
            page = await anyio.to_thread.run_sync(partial(next, pages, None))
            while page is not None:
                rows = getattr(page, "results", page)
                ideas.extend(_parse_ideas(rows, historical=False))
                # The page itself says whether another RPC is coming, so we only
                # pay for a throttle when one actually is.
                if not getattr(page, "next_page_token", ""):
                    break
                await self._rate_limiter.acquire()
                page = await anyio.to_thread.run_sync(partial(next, pages, None))
            return ideas
        except _library_exception_types() as exc:
            raise _translate_library_error(exc) from exc

    async def _call(
        self,
        service: object,
        method_name: str,
        request: dict[str, Any],
        *,
        historical: bool,
    ) -> list[KeywordIdea]:
        try:
            return await anyio.to_thread.run_sync(
                partial(
                    self._invoke_and_parse,
                    service,
                    method_name,
                    request,
                    historical=historical,
                )
            )
        except _library_exception_types() as exc:
            raise _translate_library_error(exc) from exc

    async def keyword_ideas(
        self,
        seed: AdsSeed,
        market: Market,
        *,
        include_adult: bool = False,
    ) -> list[KeywordIdea]:
        customer_id = self._require_available()
        mode = seed.mode()
        params = {
            "seed": seed.model_dump_json(),
            "mode": mode,
            "language": market.language,
            "country": market.country,
            "include_adult": json.dumps(include_adult),
        }
        cache_key = self._cache_key(IDEAS_ENDPOINT, params, customer_id)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        request: dict[str, Any] = {
            "customer_id": customer_id,
            "language": market.ads_language_resource(),
            "geo_target_constants": [market.ads_geo_target_resource()],
            "include_adult_keywords": include_adult,
            "page_size": self._settings.google_ads_page_size,
            "keyword_plan_network": _NETWORK,
        }
        if mode == "keyword_seed":
            request[mode] = {"keywords": seed.keywords}
        elif mode == "url_seed":
            request[mode] = {"url": seed.url}
        elif mode == "keyword_and_url_seed":
            request[mode] = {"keywords": seed.keywords, "url": seed.url}
        else:
            request[mode] = {"site": seed.site}

        if self._rate_limiter is None:
            raise ProviderUnavailableError("Google Ads rate limiter is not configured.")
        await self._rate_limiter.acquire()
        ideas = await self._call_ideas(self.build_service(), request)
        self._store(
            cache_key,
            IDEAS_ENDPOINT,
            customer_id,
            ideas,
            self._settings.google_ads_ideas_cache_ttl_seconds,
        )
        return ideas

    async def historical_metrics(
        self,
        keywords: Sequence[str],
        market: Market,
    ) -> list[KeywordIdea]:
        customer_id = self._require_available()
        keyword_list = list(keywords)
        if not keyword_list:
            raise InvalidConfigurationError("Google Ads historical keywords must not be empty.")
        params = {
            "keywords": json.dumps(keyword_list, ensure_ascii=False, separators=(",", ":")),
            "language": market.language,
            "country": market.country,
        }
        cache_key = self._cache_key(HISTORICAL_ENDPOINT, params, customer_id)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        request: dict[str, Any] = {
            "customer_id": customer_id,
            "keywords": keyword_list,
            "language": market.ads_language_resource(),
            "geo_target_constants": [market.ads_geo_target_resource()],
            "keyword_plan_network": _NETWORK,
        }
        if self._rate_limiter is None:
            raise ProviderUnavailableError("Google Ads rate limiter is not configured.")
        await self._rate_limiter.acquire()
        ideas = await self._call(
            self.build_service(),
            HISTORICAL_ENDPOINT,
            request,
            historical=True,
        )
        self._store(
            cache_key,
            HISTORICAL_ENDPOINT,
            customer_id,
            ideas,
            self._settings.google_ads_historical_cache_ttl_seconds,
        )
        return ideas
