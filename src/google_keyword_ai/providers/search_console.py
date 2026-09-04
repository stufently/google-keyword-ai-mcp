import json
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from functools import partial
from pathlib import Path
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
    NetworkError,
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
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    if status is None:
        # No HTTP reply at all: the credentials could not be refreshed, or the
        # request never reached Google. A refused token is an authentication
        # problem the caller can act on; anything else is the network.
        if _is_auth_error(exc):
            return AuthenticationError(f"Search Console credentials were refused: {exc}")
        return NetworkError(f"Search Console could not be reached: {exc}")
    code = int(status)
    if code in {401, 403}:
        return AuthenticationError(
            f"Search Console authentication or authorization failed ({code})."
        )
    if code == 429:
        return RateLimitError("Search Console rate limit was exceeded (429).")
    return ApiError(f"Search Console API request failed ({code}).")


def _is_auth_error(exc: BaseException) -> bool:
    try:
        from google.auth.exceptions import GoogleAuthError, TransportError
    except ImportError:  # pragma: no cover - google-auth ships with the client
        return False
    # A transport failure is reported through the auth package too, and that is
    # the network rather than the credentials.
    return isinstance(exc, GoogleAuthError) and not isinstance(exc, TransportError)


# Google accepts exactly these, case-insensitively, and treats an omitted value
# as "final". The default here used to be "full", which is not one of them.
_DATA_STATES = frozenset({"all", "final", "hourly_all"})
_DEFAULT_DATA_STATE = "final"


def _validate_data_state(value: str) -> str:
    """Refuse a `dataState` the API does not define.

    Sending an undefined value is not a local style question: the parameter
    decides whether fresh, not-yet-final rows are included, so a value Google
    does not recognise silently changes which data comes back -- or is rejected
    outright, after the request has been throttled and sent.
    """
    if value.casefold() not in _DATA_STATES:
        raise InvalidConfigurationError(
            f"Search Console dataState must be one of {', '.join(sorted(_DATA_STATES))}, "
            f"not {value!r}."
        )
    return value


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
            return self._load_with(loader, path)
        if credential_type == "authorized_user":
            from google.oauth2 import credentials

            loader = cast(Callable[..., object], credentials.Credentials.from_authorized_user_file)
            return self._load_with(loader, path)
        raise InvalidConfigurationError(
            "Search Console credentials type must be 'service_account' or 'authorized_user'."
        )

    @staticmethod
    def _load_with(loader: Callable[..., object], path: Path) -> object:
        """Build credentials, reporting a malformed file as a refused configuration.

        The type field is the only thing checked before this point, so a file
        that names a type but omits the keys that type needs gets as far as
        Google's own loader -- which raises a bare `ValueError`. That is no
        `GkaiError`, so a credentials file with a typo in it reaches the caller
        as a crash rather than as the configuration problem it is.
        """
        try:
            return loader(path, scopes=[READONLY_SCOPE])
        except ValueError as exc:
            raise InvalidConfigurationError(
                f"Search Console credentials file {path} is not usable: {exc}"
            ) from exc

    def build_service(self) -> object:
        """Build the Search Analytics client, refusing rather than crashing.

        The client is an optional extra, and `is_available()` reads only whether
        the credentials file exists -- so an installation with credentials but
        without the package answers "ready" in `gkai doctor` and then raised
        `ModuleNotFoundError` past every guard on both facades.
        """
        if self._service_factory is not None:
            return self._service_factory()

        try:
            from googleapiclient.discovery import build  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ProviderUnavailableError(
                "The google-api-python-client library is not installed; "
                "install the 'gsc' extra to use Search Console."
            ) from exc

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
        try:
            entries = cast(dict[str, Any], response).get("siteEntry", [])
            return [
                SiteProperty(
                    site_url=str(entry["siteUrl"]),
                    permission_level=str(entry["permissionLevel"]),
                )
                for entry in cast(list[dict[str, object]], entries)
            ]
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            # Both fields are read by subscript through a cast that converts
            # nothing, so a reply that stops carrying one raises `KeyError`
            # here -- no `GkaiError`, and the facades report a crash instead of
            # a provider whose answer could not be read. `_parse_rows` guards
            # the same way for the same reason. The reply's own shape is read
            # inside the guard too: a root that is not a mapping has no `get`,
            # and that `AttributeError` would otherwise escape from the line
            # above the guard.
            raise ApiError(f"Search Console property list could not be read: {exc}") from exc

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
            # The cap bounds the request itself, so it decides how many rows
            # come back and whether the answer is marked truncated. Leaving it
            # out of the key lets a run under a small cap serve its short,
            # truncated answer to a later run under a large one, which would
            # then never read the rows it was raised to fetch.
            "daily_row_cap": str(self._settings.search_console_daily_row_cap),
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
    def _parse_rows(response: object, dimensions: Sequence[str]) -> list[SearchAnalyticsRow]:
        try:
            raw_rows = cast(dict[str, Any], response).get("rows", [])
            return SearchConsoleProvider._parse_rows_strictly(
                cast(list[dict[str, object]], raw_rows), dimensions
            )
        except (ValueError, TypeError, AttributeError) as exc:
            # The casts here and below are annotations, not conversions: a reply
            # with a string where a number belongs reaches pydantic unchanged and
            # comes back as a `ValidationError`, which is a `ValueError` and no
            # `GkaiError`. Neither facade recognises that as an answer, so a
            # changed response shape reads as a crash in the tool rather than as
            # a provider that cannot be read. The reply's own shape is unwrapped
            # inside the guard for the same reason: a root that is not a mapping,
            # or a row that is not one, has no `get` and raises `AttributeError`.
            raise ApiError(f"Search Console response could not be read: {exc}") from exc

    @staticmethod
    def _parse_rows_strictly(
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
        data_state: str = _DEFAULT_DATA_STATE,
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
            data_state=_validate_data_state(data_state),
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
        capped_days: list[date] = []
        cap = self._settings.search_console_daily_row_cap
        while current_day <= end:
            start_row = 0
            # Google's own limit is "a maximum of 50K rows of data per day per
            # search type", so the allowance belongs to each day of DATA and is
            # reset here. Counted once across the whole call instead, a property
            # busier than the cap stopped the range partway through and never
            # asked for the days beyond it -- days Google would have served in
            # full.
            rows_today = 0
            while True:
                # Never ask for more than the day's remaining allowance. Asking
                # for a whole page and checking afterwards lets one page
                # overshoot by up to `page_size` rows -- 25,000 with the
                # defaults -- which makes a setting named "cap" no such thing.
                requested_rows = min(page_size, cap - rows_today)
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
                page_rows = self._parse_rows(response, requested_dimensions)
                all_rows.extend(page_rows)
                rows_today += len(page_rows)
                # A full page means another may follow for this day; a short one
                # proves the day held nothing more.
                more_to_read = len(page_rows) == requested_rows
                # Spending the whole allowance is only a truncation if it
                # stopped work that remained. A day whose last row happens to
                # land exactly on the cap was read in full, and calling that
                # incomplete sends the caller looking for data that does not
                # exist.
                if rows_today >= cap and more_to_read:
                    capped_days.append(current_day)
                    break
                if not more_to_read:
                    break
                start_row += requested_rows
            # One busy day does not end the range: every other day has its own
            # allowance, and abandoning them left the answer short of days the
            # caller asked for and Google would have served.
            current_day += timedelta(days=1)

        if capped_days:
            truncated = True
            truncation_reason = (
                f"Search Console row cap of {cap} was reached on "
                f"{len(capped_days)} of the requested days "
                f"({', '.join(day.isoformat() for day in capped_days[:5])}"
                f"{'...' if len(capped_days) > 5 else ''}); rows from those days may be "
                "missing. The cap bounds what is extracted, so a day it shrinks cannot "
                "confirm that nothing remained."
            )

        result = SearchAnalyticsPage(
            rows=_fold_days(all_rows, requested_dimensions),
            truncated=truncated,
            truncation_reason=truncation_reason,
        )
        self._store(cache_key, site_url, result)
        return result


def _fold_days(
    rows: Sequence[SearchAnalyticsRow], dimensions: Sequence[str]
) -> list[SearchAnalyticsRow]:
    """Fold the per-day requests back into the range the caller asked for.

    Splitting the window into one request per day is forced by the 25,000-row
    limit on `rowLimit`; it is an extraction strategy, not a change to the
    question. Left concatenated, the same query came back once per day, each row
    carrying that day's numbers, with no date among the keys to say so and an
    envelope labelled with the whole window. Every reader downstream then took a
    single day's figure for the range: `--limit` sliced the first day's queries,
    the opportunity thresholds tested one day's impressions against a window's
    worth of a minimum, and a qualifying query produced one duplicate
    opportunity per day it appeared.

    Clicks and impressions add. CTR is recomputed over the totals rather than
    averaged, because a mean of daily ratios is not the ratio of the sums.
    Position is averaged by impressions, which is exactly how Google averages it
    over a range -- each daily figure is itself the mean over that day's
    impressions -- so this reproduces the API's own answer rather than
    approximating it.
    """
    folded: dict[tuple[tuple[str, str], ...], list[SearchAnalyticsRow]] = {}
    for row in rows:
        folded.setdefault(tuple(sorted(row.keys.items())), []).append(row)

    merged: list[SearchAnalyticsRow] = []
    for group in folded.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        clicks = sum(row.clicks for row in group)
        impressions = sum(row.impressions for row in group)
        position = (
            sum(row.position * row.impressions for row in group) / impressions
            if impressions
            # No impressions means no weights, and a weighted mean of nothing is
            # undefined. The plain mean is the only honest answer left.
            else sum(row.position for row in group) / len(group)
        )
        merged.append(
            SearchAnalyticsRow(
                keys=group[0].keys,
                clicks=clicks,
                impressions=impressions,
                ctr=clicks / impressions if impressions else 0.0,
                position=position,
            )
        )
    # Google's own rule: "Results are sorted by click count, in descending
    # order, unless you group by date, in which case results are sorted by date,
    # in ascending order." Sorting a date-grouped answer by clicks would scramble
    # the chronology, and a `--limit` on top of it would then return the wrong
    # days rather than the first ones.
    if "date" in dimensions:
        merged.sort(key=lambda row: row.keys.get("date", ""))
    else:
        merged.sort(key=lambda row: (-row.clicks, -row.impressions, row.position))
    return merged


def _http_error_type() -> tuple[type[BaseException], ...]:
    """Every library failure a Search Console call can raise.

    `execute()` refreshes the credentials before it sends anything, so a revoked
    or expired refresh token raises `RefreshError`, and a name-resolution or
    connect failure raises out of `httplib2` -- neither of them an `HttpError`,
    neither of them a `GkaiError`. With only `HttpError` caught, a property
    whose access had been withdrawn produced a traceback and an empty stdout on
    a CLI that documents exit 1 as a verdict which still prints an envelope.
    """
    from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

    types: list[type[BaseException]] = [cast(type[BaseException], HttpError), OSError]
    try:
        from google.auth.exceptions import GoogleAuthError, TransportError
    except ImportError:  # pragma: no cover - google-auth ships with the client
        return tuple(types)
    types.extend(
        [cast(type[BaseException], GoogleAuthError), cast(type[BaseException], TransportError)]
    )
    return tuple(types)
