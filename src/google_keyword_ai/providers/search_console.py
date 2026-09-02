import json
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from functools import partial
from typing import Any, Protocol, cast

import anyio
from pydantic import BaseModel, ValidationError

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

READONLY_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
QUERY_ENDPOINT = "searchanalytics.query"


class _RateLimiter(Protocol):
    async def acquire(self) -> None: ...


class _Executable(Protocol):
    def execute(self) -> object: ...


class _SitesResource(Protocol):
    def list(self) -> _Executable: ...


class _SearchAnalyticsResource(Protocol):
    def query(self, *, siteUrl: str, body: dict[str, Any]) -> _Executable: ...


class _SearchConsoleService(Protocol):
    def sites(self) -> _SitesResource: ...

    def searchanalytics(self) -> _SearchAnalyticsResource: ...


class SearchAnalyticsRow(BaseModel):
    keys: dict[str, str]
    clicks: int
    impressions: int
    ctr: float
    position: float


class SearchAnalyticsPage(BaseModel):
    rows: list[SearchAnalyticsRow]
    truncated: bool
    truncation_reason: str | None


class SiteProperty(BaseModel):
    site_url: str
    permission_level: str


def _translate_http_error(exc: BaseException) -> GkaiError:
    status = int(cast(Any, exc).resp.status)
    if status in {401, 403}:
        return AuthenticationError(
            f"Search Console authentication or authorization failed ({status})."
        )
    if status == 429:
        return RateLimitError("Search Console rate limit was exceeded (429).")
    return ApiError(f"Search Console API request failed ({status}).")


def _as_date(value: date | str, field_name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidConfigurationError(f"{field_name} must use YYYY-MM-DD format.") from exc


class SearchConsoleProvider(Provider):
    def __init__(
        self,
        *,
        settings: Settings,
        cache: SqliteCache | None,
        rate_limiter: _RateLimiter | None,
        service_factory: Callable[[], object] | None = None,
    ) -> None:
        self._settings = settings
        self._cache = cache
        self._rate_limiter = rate_limiter
        self._service_factory = service_factory

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(name="search_console", official=True, stability="stable")

    def is_available(self) -> bool:
        path = self._settings.search_console_credentials_path
        return path is not None and path.is_file()

    def _require_available(self) -> None:
        if not self.is_available():
            raise ProviderUnavailableError(
                "Search Console credentials are missing or the credentials file does not exist."
            )

    def load_credentials(self) -> object:
        path = self._settings.search_console_credentials_path
        if path is None:
            raise ProviderUnavailableError("Search Console credentials path is not configured.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidConfigurationError(
                f"Unable to read Search Console credentials file {path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise InvalidConfigurationError(
                "Search Console credentials file must contain a JSON object."
            )

        credential_type = payload.get("type")
        if credential_type == "service_account":
            from google.oauth2 import service_account

            loader = cast(
                Callable[..., object], service_account.Credentials.from_service_account_file
            )
            return loader(path, scopes=[READONLY_SCOPE])
        if credential_type == "authorized_user":
            from google.oauth2 import credentials

            loader = cast(Callable[..., object], credentials.Credentials.from_authorized_user_file)
            return loader(path, scopes=[READONLY_SCOPE])
        raise InvalidConfigurationError(
            "Search Console credentials type must be 'service_account' or 'authorized_user'."
        )

    def build_service(self) -> object:
        if self._service_factory is not None:
            return self._service_factory()

        from googleapiclient.discovery import build  # type: ignore[import-untyped]

        return build(
            "searchconsole",
            "v1",
            credentials=self.load_credentials(),
            static_discovery=True,
            cache_discovery=False,
        )

    def _require_rate_limiter(self) -> _RateLimiter:
        if self._rate_limiter is None:
            raise ProviderUnavailableError("Search Console rate limiter is not configured.")
        return self._rate_limiter

    @staticmethod
    def _execute_properties(service: object) -> object:
        return cast(_SearchConsoleService, service).sites().list().execute()

    @staticmethod
    def _execute_query(service: object, site_url: str, body: dict[str, Any]) -> object:
        request = (
            cast(_SearchConsoleService, service)
            .searchanalytics()
            .query(
                siteUrl=site_url,
                body=body,
            )
        )
        return request.execute()

    async def list_properties(self) -> list[SiteProperty]:
        self._require_available()
        await self._require_rate_limiter().acquire()
        try:
            response = await anyio.to_thread.run_sync(
                partial(self._execute_properties, self.build_service())
            )
        except _http_error_type() as exc:
            raise _translate_http_error(exc) from exc
        entries = cast(dict[str, Any], response).get("siteEntry", [])
        return [
            SiteProperty(
                site_url=str(entry["siteUrl"]),
                permission_level=str(entry["permissionLevel"]),
            )
            for entry in cast(list[dict[str, object]], entries)
        ]

    def _cache_key(
        self,
        site_url: str,
        *,
        start_date: date,
        end_date: date,
        dimensions: list[str],
        market: Market | None,
        search_type: str,
        data_state: str,
        row_limit: int,
        dimension_filters: list[dict[str, str]],
    ) -> str:
        params = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "dimensions": json.dumps(dimensions, separators=(",", ":")),
            "country": "" if market is None else market.gsc_country(),
            "search_type": search_type,
            "data_state": data_state,
            "row_limit": str(row_limit),
            "authorization_scope": READONLY_SCOPE,
            "dimension_filters": json.dumps(
                dimension_filters,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        return build_cache_key(
            self.info.name,
            QUERY_ENDPOINT,
            params,
            account_scope=site_url,
            parser_version=PARSER_VERSION,
        )

    def _cached(self, key: str) -> SearchAnalyticsPage | None:
        if self._cache is None:
            raise ProviderUnavailableError("Search Console cache is not configured.")
        payload = self._cache.get(key)
        if payload is None:
            return None
        try:
            return SearchAnalyticsPage.model_validate_json(payload)
        except ValidationError as exc:
            raise ApiError("Search Console cache entry is invalid.") from exc

    def _store(self, key: str, site_url: str, page: SearchAnalyticsPage) -> None:
        if self._cache is None:
            raise ProviderUnavailableError("Search Console cache is not configured.")
        self._cache.set(
            key,
            provider=self.info.name,
            endpoint=QUERY_ENDPOINT,
            account_scope=site_url,
            parser_version=PARSER_VERSION,
            payload=page.model_dump_json().encode(),
            ttl_seconds=self._settings.search_console_cache_ttl_seconds,
        )

    @staticmethod
    def _parse_rows(
        raw_rows: Sequence[dict[str, object]], dimensions: Sequence[str]
    ) -> list[SearchAnalyticsRow]:
        parsed: list[SearchAnalyticsRow] = []
        for raw in raw_rows:
            raw_keys = cast(list[object], raw.get("keys", []))
            if len(raw_keys) != len(dimensions):
                raise ApiError(
                    "Search Console response key count does not match requested dimensions."
                )
            parsed.append(
                SearchAnalyticsRow(
                    keys={
                        name: str(value) for name, value in zip(dimensions, raw_keys, strict=True)
                    },
                    clicks=cast(int, raw.get("clicks", 0)),
                    impressions=cast(int, raw.get("impressions", 0)),
                    ctr=cast(float, raw.get("ctr", 0.0)),
                    position=cast(float, raw.get("position", 0.0)),
                )
            )
        return parsed

    async def query(
        self,
        site_url: str,
        *,
        start_date: date | str,
        end_date: date | str,
        dimensions: Sequence[str],
        market: Market | None = None,
        search_type: str = "web",
        data_state: str = "full",
        row_limit: int | None = None,
        dimension_filters: Sequence[dict[str, str]] | None = None,
    ) -> SearchAnalyticsPage:
        self._require_available()
        start = _as_date(start_date, "start_date")
        end = _as_date(end_date, "end_date")
        if start > end:
            raise InvalidConfigurationError("start_date must not be after end_date.")
        page_size = self._settings.search_console_row_limit if row_limit is None else row_limit
        if not 1 <= page_size <= 25000:
            raise InvalidConfigurationError("row_limit must be between 1 and 25000.")

        requested_dimensions = list(dimensions)
        filters = [] if dimension_filters is None else [dict(item) for item in dimension_filters]
        if market is not None:
            filters.append(
                {
                    "dimension": "country",
                    "operator": "equals",
                    "expression": market.gsc_country(),
                }
            )
        cache_key = self._cache_key(
            site_url,
            start_date=start,
            end_date=end,
            dimensions=requested_dimensions,
            market=market,
            search_type=search_type,
            data_state=data_state,
            row_limit=page_size,
            dimension_filters=filters,
        )
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        service = self.build_service()
        limiter = self._require_rate_limiter()
        all_rows: list[SearchAnalyticsRow] = []
        truncated = False
        truncation_reason: str | None = None
        current_day = start
        # The cap counts rows pulled by THIS call, not rows per day of data:
        # Google budgets extraction per property per calendar day of requests, so
        # a 28-day window that reset the counter every day would quietly spend
        # 28 budgets and still report a complete answer.
        rows_fetched = 0
        while current_day <= end:
            start_row = 0
            while True:
                # Never ask for more than the cap still allows. Asking for a
                # whole page and checking afterwards lets one page overshoot by
                # up to `page_size` rows -- 25,000 with the defaults -- which
                # makes a setting named "cap" no such thing.
                requested_rows = min(
                    page_size, self._settings.search_console_daily_row_cap - rows_fetched
                )
                body: dict[str, Any] = {
                    "startDate": current_day.isoformat(),
                    "endDate": current_day.isoformat(),
                    "dimensions": requested_dimensions,
                    "rowLimit": requested_rows,
                    "startRow": start_row,
                    "type": search_type,
                    "dataState": data_state,
                }
                if filters:
                    body["dimensionFilterGroups"] = [{"groupType": "and", "filters": filters}]
                await limiter.acquire()
                try:
                    response = await anyio.to_thread.run_sync(
                        partial(self._execute_query, service, site_url, body)
                    )
                except _http_error_type() as exc:
                    raise _translate_http_error(exc) from exc
                raw_rows = cast(dict[str, Any], response).get("rows", [])
                page_rows = self._parse_rows(
                    cast(list[dict[str, object]], raw_rows), requested_dimensions
                )
                all_rows.extend(page_rows)
                rows_fetched += len(page_rows)
                # A full page means another may follow; a short one ends the day.
                more_to_read = len(page_rows) == requested_rows or current_day < end
                # Spending the whole cap is only a truncation if it stopped work
                # that remained. A range whose last row happens to land exactly
                # on the cap was read in full, and calling that incomplete
                # sends the caller looking for data that does not exist.
                if rows_fetched >= self._settings.search_console_daily_row_cap and more_to_read:
                    truncated = True
                    truncation_reason = (
                        "Search Console daily row cap of "
                        f"{self._settings.search_console_daily_row_cap} reached while reading "
                        f"{start.isoformat()}..{end.isoformat()} (stopped at "
                        f"{current_day.isoformat()}); rows may be missing. The cap bounds what "
                        "is extracted, so a request it shrinks cannot confirm that nothing "
                        "remained."
                    )
                    break
                if len(page_rows) < requested_rows:
                    break
                start_row += requested_rows
            if truncated:
                break
            current_day += timedelta(days=1)

        result = SearchAnalyticsPage(
            rows=all_rows,
            truncated=truncated,
            truncation_reason=truncation_reason,
        )
        self._store(cache_key, site_url, result)
        return result


def _http_error_type() -> type[BaseException]:
    from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

    return cast(type[BaseException], HttpError)
