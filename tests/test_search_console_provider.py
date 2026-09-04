import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import anyio.to_thread
import pytest
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
from sqlalchemy.engine import Engine

from google_keyword_ai.cache import SqliteCache
from google_keyword_ai.config import Settings
from google_keyword_ai.errors import (
    ApiError,
    AuthenticationError,
    InvalidConfigurationError,
    ProviderUnavailableError,
    RateLimitError,
)
from google_keyword_ai.market import Market
from google_keyword_ai.providers.search_console import SearchAnalyticsPage, SearchConsoleProvider
from google_keyword_ai.storage.engine import open_database


async def _working_thread_runner[T](function: Callable[..., T], *args: object) -> T:
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(function, *args).result()


class ImmediateRateLimiter:
    async def acquire(self) -> None:
        return None


def _row(query: str = "keyword", page: str = "https://example.com/page") -> dict[str, object]:
    return {
        "keys": [query, page],
        "clicks": 3,
        "impressions": 120,
        "ctr": 0.025,
        "position": 8.5,
    }


class FakeRequest:
    def __init__(self, service: "FakeService", response: object) -> None:
        self.service = service
        self.response = response

    def execute(self) -> object:
        self.service.thread_ids.append(threading.get_ident())
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class FakeService:
    def __init__(
        self,
        responses: list[object] | None = None,
        properties: list[dict[str, str]] | None = None,
    ) -> None:
        self.responses = [] if responses is None else list(responses)
        self.properties = [] if properties is None else properties
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.thread_ids: list[int] = []

    def searchanalytics(self) -> "FakeService":
        return self

    def query(self, *, siteUrl: str, body: dict[str, Any]) -> FakeRequest:
        self.calls.append((siteUrl, body))
        response = self.responses.pop(0) if self.responses else {"rows": []}
        return FakeRequest(self, response)

    def sites(self) -> "FakeService":
        return self

    def list(self) -> FakeRequest:
        return FakeRequest(self, {"siteEntry": self.properties})


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    credentials = tmp_path / "credentials.json"
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text("{}", encoding="utf-8")
    values = {
        "data_dir": tmp_path / "data",
        "search_console_credentials_path": credentials,
        **overrides,
    }
    return Settings.model_validate(values)


def _provider(settings: Settings, service: FakeService) -> tuple[SearchConsoleProvider, Engine]:
    engine = open_database(settings)
    return (
        SearchConsoleProvider(
            settings=settings,
            cache=SqliteCache(engine, settings),
            rate_limiter=ImmediateRateLimiter(),
            service_factory=lambda: service,
        ),
        engine,
    )


def _query(
    provider: SearchConsoleProvider,
    site_url: str,
    *,
    start_date: date,
    end_date: date,
    dimensions: list[str],
    row_limit: int | None = None,
    market: Market | None = None,
    search_type: str = "web",
) -> SearchAnalyticsPage:
    return anyio.run(
        partial(
            provider.query,
            site_url,
            start_date=start_date,
            end_date=end_date,
            dimensions=dimensions,
            row_limit=row_limit,
            market=market,
            search_type=search_type,
        )
    )


@pytest.fixture(autouse=True)
def working_thread_offload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(anyio.to_thread, "run_sync", _working_thread_runner)


def test_keys_follow_requested_dimensions_in_order(tmp_path: Path) -> None:
    service = FakeService([{"rows": [_row()]}])
    provider, engine = _provider(_settings(tmp_path), service)
    try:
        result = _query(
            provider,
            "sc-domain:example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            dimensions=["query", "page"],
        )
    finally:
        engine.dispose()

    assert result.rows[0].keys == {
        "query": "keyword",
        "page": "https://example.com/page",
    }


def test_a_row_that_cannot_be_read_is_an_api_error(tmp_path: Path) -> None:
    """A changed response shape is a provider fault, not a crash in the tool.

    The casts around this parse are annotations, not conversions: a reply with
    a string where a number belongs travels untouched into pydantic and comes
    back a `ValidationError`, which is a `ValueError` and no `GkaiError`. Both
    facades watch for `GkaiError`, so the caller would get a traceback or an
    opaque tool failure in place of a stated reason.
    """
    broken = _row()
    broken["clicks"] = "many"
    service = FakeService([{"rows": [broken]}])
    provider, engine = _provider(_settings(tmp_path), service)
    try:
        with pytest.raises(ApiError) as raised:
            _query(
                provider,
                "sc-domain:example.com",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 1),
                dimensions=["query", "page"],
            )
    finally:
        engine.dispose()

    assert "Search Console response could not be read" in raised.value.message


def test_daily_range_is_split_into_one_request_per_day(tmp_path: Path) -> None:
    service = FakeService([{"rows": []}, {"rows": []}, {"rows": []}])
    provider, engine = _provider(_settings(tmp_path), service)
    try:
        _query(
            provider,
            "sc-domain:example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 3),
            dimensions=["query"],
        )
    finally:
        engine.dispose()

    assert [(call[1]["startDate"], call[1]["endDate"]) for call in service.calls] == [
        ("2026-08-01", "2026-08-01"),
        ("2026-08-02", "2026-08-02"),
        ("2026-08-03", "2026-08-03"),
    ]


def test_paging_uses_start_row_until_a_short_page(tmp_path: Path) -> None:
    first = _row()
    first["keys"] = ["one"]
    second = _row()
    second["keys"] = ["two"]
    last = _row()
    last["keys"] = ["three"]
    service = FakeService([{"rows": [first, second]}, {"rows": [last]}])
    provider, engine = _provider(_settings(tmp_path), service)
    try:
        result = _query(
            provider,
            "sc-domain:example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            dimensions=["query"],
            row_limit=2,
        )
    finally:
        engine.dispose()

    assert [body["startRow"] for _, body in service.calls] == [0, 2]
    assert len(result.rows) == 3


def test_paging_rejects_row_limit_above_api_maximum(tmp_path: Path) -> None:
    provider, engine = _provider(_settings(tmp_path), FakeService())
    try:
        with pytest.raises(InvalidConfigurationError, match="25000"):
            _query(
                provider,
                "sc-domain:example.com",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 1),
                dimensions=["query"],
                row_limit=25001,
            )
    finally:
        engine.dispose()


def test_truncation_is_explicit_at_daily_cap_and_stops_fetching(tmp_path: Path) -> None:
    first = _row()
    first["keys"] = ["one"]
    second = _row()
    second["keys"] = ["two"]
    service = FakeService([{"rows": [first, second]}, {"rows": []}])
    settings = _settings(tmp_path, search_console_daily_row_cap=2)
    provider, engine = _provider(settings, service)
    try:
        result = _query(
            provider,
            "sc-domain:example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 2),
            dimensions=["query"],
            row_limit=2,
        )
    finally:
        engine.dispose()

    assert result.truncated is True
    assert result.truncation_reason is not None
    assert "2" in result.truncation_reason
    assert len(service.calls) == 1


def test_truncation_cap_counts_across_days_not_per_day(tmp_path: Path) -> None:
    """The cap is a budget for the whole call, not a fresh allowance each day.

    Google budgets extraction per property per calendar day of requests. A
    counter that resets on every day of DATA would let a 28-day window spend
    that budget 28 times over and still call the answer complete. Two days that
    return one row each must trip a cap of two.
    """
    first = _row()
    first["keys"] = ["day-one"]
    second = _row()
    second["keys"] = ["day-two"]
    service = FakeService([{"rows": [first]}, {"rows": [second]}, {"rows": []}])
    settings = _settings(tmp_path, search_console_daily_row_cap=2)
    provider, engine = _provider(settings, service)
    try:
        result = _query(
            provider,
            "sc-domain:example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 3),
            dimensions=["query"],
            row_limit=10,
        )
    finally:
        engine.dispose()

    assert result.truncated is True
    assert [row.keys["query"] for row in result.rows] == ["day-one", "day-two"]
    # Day three is never requested: the budget ran out on day two.
    assert len(service.calls) == 2


def test_a_day_that_ends_before_the_cap_is_complete(tmp_path: Path) -> None:
    """Spending part of the cap is not the same as being cut off by it.

    A short page proves the day held nothing more, and on the last day of the
    range that makes the answer whole. Reporting it as truncated would make the
    run partial and send the caller hunting for rows that were never withheld.
    """
    first = _row()
    first["keys"] = ["day-one"]
    second = _row()
    second["keys"] = ["day-two"]
    service = FakeService([{"rows": [first]}, {"rows": [second]}])
    settings = _settings(tmp_path, search_console_daily_row_cap=50)
    provider, engine = _provider(settings, service)
    try:
        result = _query(
            provider,
            "sc-domain:example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 2),
            dimensions=["query"],
            row_limit=10,
        )
    finally:
        engine.dispose()

    assert result.truncated is False
    assert result.truncation_reason is None
    assert [row.keys["query"] for row in result.rows] == ["day-one", "day-two"]
    assert len(service.calls) == 2


def test_the_cap_bounds_what_is_asked_for_not_only_what_is_counted(tmp_path: Path) -> None:
    """A cap checked after the fact is not a cap.

    The cap exists to bound extraction on Google's side, so asking for a whole
    page when the cap allows two more rows spends budget the caller forbade --
    up to 25,000 rows past it with the defaults. The request itself has to
    shrink, and a request the cap shrinks cannot prove nothing remained, so the
    answer stays truncated.
    """
    rows = []
    for index in range(3):
        row = _row()
        row["keys"] = [f"row-{index}"]
        rows.append(row)
    service = FakeService([{"rows": rows[:2]}])
    settings = _settings(tmp_path, search_console_daily_row_cap=2)
    provider, engine = _provider(settings, service)
    try:
        result = _query(
            provider,
            "sc-domain:example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            dimensions=["query"],
            row_limit=10,
        )
    finally:
        engine.dispose()

    assert [body["rowLimit"] for _, body in service.calls] == [2], (
        "the cap, not the page size, decides how many rows may be asked for"
    )
    assert len(result.rows) == 2
    assert result.truncated is True


def _http_error(status: int) -> HttpError:
    return HttpError(SimpleNamespace(status=status, reason="test"), b"{}")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param(403, AuthenticationError, id="errors_403_authentication"),
        pytest.param(429, RateLimitError, id="errors_429_rate_limit"),
        pytest.param(500, ApiError, id="errors_500_api"),
    ],
)
def test_errors_are_translated_by_http_status(
    status: int,
    expected: type[Exception],
    tmp_path: Path,
) -> None:
    provider, engine = _provider(_settings(tmp_path), FakeService([_http_error(status)]))
    try:
        with pytest.raises(expected):
            _query(
                provider,
                "sc-domain:example.com",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 1),
                dimensions=["query"],
            )
    finally:
        engine.dispose()


def test_cache_is_scoped_by_site_url_and_reused(tmp_path: Path) -> None:
    service = FakeService([{"rows": []}, {"rows": []}])
    provider, engine = _provider(_settings(tmp_path), service)

    def call(site_url: str) -> None:
        _query(
            provider,
            site_url,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            dimensions=["query"],
        )

    try:
        call("sc-domain:first.example")
        call("sc-domain:first.example")
        call("sc-domain:second.example")
    finally:
        engine.dispose()

    assert [site_url for site_url, _ in service.calls] == [
        "sc-domain:first.example",
        "sc-domain:second.example",
    ]


def test_blocking_execute_runs_in_worker_thread(tmp_path: Path) -> None:
    service = FakeService([{"rows": []}])
    provider, engine = _provider(_settings(tmp_path), service)
    event_loop_thread = threading.get_ident()
    try:
        _query(
            provider,
            "sc-domain:example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            dimensions=["query"],
        )
    finally:
        engine.dispose()

    assert service.thread_ids
    assert all(thread_id != event_loop_thread for thread_id in service.thread_ids)


def test_missing_credentials_make_provider_unavailable(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    provider = SearchConsoleProvider(
        settings=settings,
        cache=None,
        rate_limiter=ImmediateRateLimiter(),
        service_factory=lambda: FakeService(),
    )

    assert provider.is_available() is False
    with pytest.raises(ProviderUnavailableError, match="credentials"):
        _query(
            provider,
            "sc-domain:example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            dimensions=["query"],
        )


def test_unknown_credentials_type_is_invalid_configuration(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.search_console_credentials_path is not None
    settings.search_console_credentials_path.write_text(
        json.dumps({"type": "external_account"}), encoding="utf-8"
    )
    provider = SearchConsoleProvider(settings=settings, cache=None, rate_limiter=None)

    with pytest.raises(InvalidConfigurationError, match=r"service_account.*authorized_user"):
        provider.load_credentials()


def test_properties_and_country_filter_use_official_shapes(tmp_path: Path) -> None:
    service = FakeService(
        [{"rows": []}],
        properties=[{"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"}],
    )
    provider, engine = _provider(_settings(tmp_path), service)
    try:
        properties = anyio.run(provider.list_properties)
        _query(
            provider,
            "sc-domain:example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            dimensions=["query"],
            market=Market.parse("en", "US"),
            search_type="image",
        )
    finally:
        engine.dispose()

    assert properties[0].site_url == "sc-domain:example.com"
    body = service.calls[0][1]
    assert body["type"] == "image"
    assert body["dimensionFilterGroups"][0]["filters"][0]["expression"] == "usa"


def test_the_row_cap_is_part_of_the_cache_key(tmp_path: Path) -> None:
    """A short answer produced under a small cap must not be served to a large one.

    The cap bounds the request itself, so it decides how many rows come back
    and whether the answer is marked truncated -- it is an input to the result,
    not a detail of how it was fetched. Left out of the key, raising the cap
    changed nothing: the run kept being handed the truncated answer it was
    raised to replace, and the extra rows were never read.
    """
    first = _row()
    first["keys"] = ["one"]
    second = _row()
    second["keys"] = ["two"]

    small = _settings(tmp_path, search_console_daily_row_cap=1)
    service = FakeService([{"rows": [first]}])
    provider, engine = _provider(small, service)
    try:
        capped = _query(
            provider,
            "sc-domain:example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            dimensions=["query"],
            row_limit=10,
        )
    finally:
        engine.dispose()

    assert capped.truncated is True
    assert len(capped.rows) == 1

    large = _settings(tmp_path, search_console_daily_row_cap=50)
    reopened = FakeService([{"rows": [first, second]}, {"rows": []}])
    provider, engine = _provider(large, reopened)
    try:
        raised = _query(
            provider,
            "sc-domain:example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            dimensions=["query"],
            row_limit=10,
        )
    finally:
        engine.dispose()

    assert reopened.calls, "the raised cap reused the answer capped at one row"
    assert raised.truncated is False
    assert [row.keys["query"] for row in raised.rows] == ["one", "two"]


def test_a_property_that_cannot_be_read_is_an_api_error(tmp_path: Path) -> None:
    """A missing field in the property list must not surface as a crash.

    Rows already report an unreadable reply as `ApiError`; the property list
    reads its two fields by subscript and was left out. A `KeyError` is no
    `GkaiError`, so a reply that stops carrying `permissionLevel` reaches the
    facades as an opaque tool failure rather than as a provider whose answer
    could not be read.
    """
    service = FakeService(properties=[{"siteUrl": "sc-domain:example.com"}])
    provider, engine = _provider(_settings(tmp_path), service)
    try:
        with pytest.raises(ApiError) as raised:
            anyio.run(provider.list_properties)
    finally:
        engine.dispose()

    assert "Search Console property list could not be read" in raised.value.message


def test_a_property_reply_that_is_not_an_object_is_an_api_error(tmp_path: Path) -> None:
    """The reply's own shape is part of what has to be read.

    `siteEntry` is pulled off the root with `.get`, through a cast that converts
    nothing. A root that is not a mapping has no `get` at all, and that
    `AttributeError` is no `GkaiError` either -- so the shape check belongs
    under the same guard as the fields it protects.
    """

    class ListShapedService(FakeService):
        def list(self) -> FakeRequest:
            return FakeRequest(self, ["sc-domain:example.com"])

    provider, engine = _provider(_settings(tmp_path), ListShapedService())
    try:
        with pytest.raises(ApiError) as raised:
            anyio.run(provider.list_properties)
    finally:
        engine.dispose()

    assert "Search Console property list could not be read" in raised.value.message


def test_a_query_reply_that_is_not_an_object_is_an_api_error(tmp_path: Path) -> None:
    """The row reply's shape is read under the same guard as the rows.

    `rows` used to be pulled off the root before `_parse_rows` was entered, so a
    root that is not a mapping raised a bare `AttributeError` one line short of
    the guard written to catch exactly that kind of change.
    """
    service = FakeService([["not", "an", "object"]])
    provider, engine = _provider(_settings(tmp_path), service)
    try:
        with pytest.raises(ApiError) as raised:
            _query(
                provider,
                "sc-domain:example.com",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 1),
                dimensions=["query"],
            )
    finally:
        engine.dispose()

    assert "Search Console response could not be read" in raised.value.message


def test_a_credentials_file_google_rejects_is_a_configuration_error(tmp_path: Path) -> None:
    """A typo in the credentials file is a configuration problem, not a crash.

    Only the `type` field is checked here; everything the type actually needs is
    checked by Google's own loader, which raises a bare `ValueError`. That is no
    `GkaiError`, so a service-account file missing its key reached the caller as
    a traceback instead of the refusal envelope both facades promise.
    """
    settings = _settings(tmp_path)
    assert settings.search_console_credentials_path is not None
    settings.search_console_credentials_path.write_text(
        json.dumps({"type": "service_account", "client_email": "nobody@example.com"}),
        encoding="utf-8",
    )
    provider = SearchConsoleProvider(settings=settings, cache=None, rate_limiter=None)

    with pytest.raises(InvalidConfigurationError) as raised:
        provider.load_credentials()

    assert "is not usable" in raised.value.message


def test_a_reply_without_a_rows_key_is_an_empty_page(tmp_path: Path) -> None:
    """Search Console omits `rows` entirely when a range has no data.

    That is an ordinary empty answer, not a malformed reply, so the key is read
    with a default rather than by subscript -- reading it by subscript would
    turn every quiet day into an error.
    """
    service = FakeService([{}])
    provider, engine = _provider(_settings(tmp_path), service)
    try:
        page = _query(
            provider,
            "sc-domain:example.com",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            dimensions=["query"],
        )
    finally:
        engine.dispose()

    assert page.rows == []
    assert not page.truncated
