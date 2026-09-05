import builtins
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import anyio
import anyio.to_thread
import pytest
from google.ads.googleads.errors import GoogleAdsException  # type: ignore[import-untyped]
from google.api_core.exceptions import InternalServerError, Unauthenticated
from pydantic import SecretStr
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
from google_keyword_ai.providers.google_ads import AdsSeed, GoogleAdsProvider, KeywordIdeaPage
from google_keyword_ai.ratelimit import InterProcessRateLimiter
from google_keyword_ai.storage.engine import open_database


async def _working_thread_runner[T](function: Callable[..., T], *args: object) -> T:
    """Run a worker deterministically where sandbox socket wakeups are forbidden."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(function, *args).result()


class ImmediateRateLimiter(InterProcessRateLimiter):
    def __init__(self, lock_path: Path) -> None:
        super().__init__(1.0, lock_path)

    async def acquire(self) -> None:
        return None


def _credentials(data_dir: Path, customer_id: str = "1234567890") -> Settings:
    return Settings(
        data_dir=data_dir,
        google_ads_developer_token=SecretStr("developer-token"),
        google_ads_customer_id=customer_id,
        google_ads_client_id=SecretStr("client-id"),
        google_ads_client_secret=SecretStr("client-secret"),
        google_ads_refresh_token=SecretStr("refresh-token"),
    )


def _metrics() -> SimpleNamespace:
    return SimpleNamespace(
        avg_monthly_searches=120,
        monthly_search_volumes=[
            SimpleNamespace(year=2026, month=SimpleNamespace(name="AUGUST"), monthly_searches=130)
        ],
        competition=SimpleNamespace(name="HIGH"),
        competition_index=87,
        low_top_of_page_bid_micros=1_500_000,
        high_top_of_page_bid_micros=2_750_000,
        average_cpc_micros=1_250_000,
    )


def _idea(text: str = "keyword") -> SimpleNamespace:
    return SimpleNamespace(text=text, keyword_idea_metrics=_metrics())


class FakeService:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.thread_ids: list[int] = []

    def generate_keyword_ideas(self, *, request: dict[str, Any]) -> Any:
        self.calls.append(("ideas", request))
        self.thread_ids.append(threading.get_ident())
        if self.error is not None:
            raise self.error
        return [_idea()]

    def generate_keyword_historical_metrics(self, *, request: dict[str, Any]) -> SimpleNamespace:
        self.calls.append(("historical", request))
        self.thread_ids.append(threading.get_ident())
        if self.error is not None:
            raise self.error
        idea = _idea()
        return SimpleNamespace(
            results=[SimpleNamespace(text=idea.text, keyword_metrics=idea.keyword_idea_metrics)]
        )


class FakeStatus(Enum):
    RESOURCE_EXHAUSTED = 1


class FakeRpcError:
    def code(self) -> FakeStatus:
        return FakeStatus.RESOURCE_EXHAUSTED


_QUOTA_ERROR = cast(Callable[[object, object, object, str], BaseException], GoogleAdsException)(
    FakeRpcError(), object(), object(), "request-id"
)


def _provider(settings: Settings, service: FakeService) -> tuple[GoogleAdsProvider, Engine]:
    engine = open_database(settings)
    provider = GoogleAdsProvider(
        settings=settings,
        cache=SqliteCache(engine, settings),
        rate_limiter=ImmediateRateLimiter(settings.data_dir / "ads.lock"),
        service_factory=lambda: service,
    )
    return provider, engine


@pytest.fixture(autouse=True)
def working_thread_offload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(anyio.to_thread, "run_sync", _working_thread_runner)


def test_micros_are_converted_to_currency_units(tmp_path: Path) -> None:
    provider, engine = _provider(_credentials(tmp_path / "data"), FakeService())
    try:
        page = anyio.run(
            provider.keyword_ideas, AdsSeed(keywords=["keyword"]), Market.parse("en", "US")
        )
    finally:
        engine.dispose()

    assert page.ideas[0].metrics.low_top_of_page_bid == 1.5
    assert page.ideas[0].metrics.high_top_of_page_bid == 2.75
    assert page.ideas[0].metrics.average_cpc == 1.25
    assert page.ideas[0].metrics.currency is None


@pytest.mark.parametrize(
    ("seed", "expected_mode", "expected_payload"),
    [
        (AdsSeed(keywords=["one"]), "keyword_seed", {"keywords": ["one"]}),
        (AdsSeed(url="https://example.com/page"), "url_seed", {"url": "https://example.com/page"}),
        (
            AdsSeed(keywords=["one"], url="https://example.com/page"),
            "keyword_and_url_seed",
            {"keywords": ["one"], "url": "https://example.com/page"},
        ),
        (AdsSeed(site="example.com"), "site_seed", {"site": "example.com"}),
    ],
    ids=["keyword_seed", "url_seed", "keyword_and_url_seed", "site_seed"],
)
def test_seed_modes(
    seed: AdsSeed,
    expected_mode: str,
    expected_payload: dict[str, object],
    tmp_path: Path,
) -> None:
    service = FakeService()
    provider, engine = _provider(_credentials(tmp_path / expected_mode), service)
    try:
        anyio.run(provider.keyword_ideas, seed, Market.parse("en", "US"))
    finally:
        engine.dispose()

    request = service.calls[0][1]
    assert seed.mode() == expected_mode
    assert request[expected_mode] == expected_payload
    assert (
        sum(
            name in request
            for name in {"keyword_seed", "url_seed", "keyword_and_url_seed", "site_seed"}
        )
        == 1
    )


def test_seed_rejects_site_with_keywords() -> None:
    with pytest.raises(InvalidConfigurationError, match="site seed"):
        AdsSeed(site="example.com", keywords=["one"]).mode()


def test_seed_rejects_empty_configuration() -> None:
    with pytest.raises(InvalidConfigurationError, match="must not be empty"):
        AdsSeed().mode()


def test_missing_credentials_are_rejected_without_calling_service(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    service = FakeService()
    provider, engine = _provider(settings, service)
    try:
        assert provider.is_available() is False
        with pytest.raises(ProviderUnavailableError, match="credentials"):
            anyio.run(
                provider.keyword_ideas,
                AdsSeed(keywords=["one"]),
                Market.parse("en", "US"),
            )
    finally:
        engine.dispose()

    assert service.calls == []


def test_cache_is_scoped_by_customer_and_reused(tmp_path: Path) -> None:
    data_dir = tmp_path / "shared"
    service = FakeService()
    first, first_engine = _provider(_credentials(data_dir, "111"), service)
    second, second_engine = _provider(_credentials(data_dir, "222"), service)
    seed = AdsSeed(keywords=["one"])
    market = Market.parse("en", "US")
    try:
        anyio.run(first.keyword_ideas, seed, market)
        anyio.run(second.keyword_ideas, seed, market)
        anyio.run(first.keyword_ideas, seed, market)
    finally:
        first_engine.dispose()
        second_engine.dispose()

    assert len(service.calls) == 2


def test_the_api_version_is_part_of_the_cache_key(tmp_path: Path) -> None:
    """A stored answer belongs to the API version that produced it.

    The version picks which API the client talks to, and Google changes what a
    version returns. `parser_version` covers our reading of the reply and says
    nothing about theirs, so without the version in the key a run pinned to the
    new API is quietly served the old one's answer.
    """
    data_dir = tmp_path / "shared"
    service = FakeService()
    settings = _credentials(data_dir)
    older, older_engine = _provider(settings, service)
    newer, newer_engine = _provider(
        settings.model_copy(update={"google_ads_api_version": "v26"}), service
    )
    seed = AdsSeed(keywords=["one"])
    market = Market.parse("en", "US")
    try:
        anyio.run(older.keyword_ideas, seed, market)
        anyio.run(newer.keyword_ideas, seed, market)
    finally:
        older_engine.dispose()
        newer_engine.dispose()

    assert len(service.calls) == 2, "the second version was answered from the first version's cache"


def test_a_reply_that_cannot_be_read_is_an_api_error(tmp_path: Path) -> None:
    """A changed response shape is a provider fault, not a crash in the tool.

    Only Google's own API errors are translated around the call; reading the
    reply is not covered. A field that stops being a number raises a plain
    `ValueError` here, which is no `GkaiError` -- and both facades watch for
    `GkaiError`, so the caller would get a traceback or an opaque tool failure
    instead of a stated reason.
    """
    service = FakeService()
    broken = _metrics()
    broken.avg_monthly_searches = "many"
    service.generate_keyword_ideas = (  # type: ignore[method-assign]
        lambda *, request: [SimpleNamespace(text="keyword", keyword_idea_metrics=broken)]
    )
    provider, engine = _provider(_credentials(tmp_path), service)
    try:
        with pytest.raises(ApiError) as raised:
            anyio.run(provider.keyword_ideas, AdsSeed(keywords=["one"]), Market.parse("en", "US"))
    finally:
        engine.dispose()

    assert "Google Ads response could not be read" in raised.value.message


@pytest.mark.parametrize(
    ("library_error", "expected_error"),
    [
        (_QUOTA_ERROR, RateLimitError),
        (cast(Callable[[str], BaseException], Unauthenticated)("credentials"), AuthenticationError),
        (cast(Callable[[str], BaseException], InternalServerError)("server"), ApiError),
    ],
)
def test_library_errors_map_to_project_taxonomy(
    library_error: BaseException,
    expected_error: type[BaseException],
    tmp_path: Path,
) -> None:
    provider, engine = _provider(
        _credentials(tmp_path / expected_error.__name__), FakeService(error=library_error)
    )
    try:
        with pytest.raises(expected_error):
            anyio.run(
                provider.keyword_ideas,
                AdsSeed(keywords=["one"]),
                Market.parse("en", "US"),
            )
    finally:
        engine.dispose()


def test_service_call_runs_outside_event_loop_thread(tmp_path: Path) -> None:
    service = FakeService()
    provider, engine = _provider(_credentials(tmp_path / "thread"), service)

    async def invoke() -> int:
        loop_thread = threading.get_ident()
        await provider.keyword_ideas(AdsSeed(keywords=["one"]), Market.parse("en", "US"))
        return loop_thread

    try:
        loop_thread = anyio.run(invoke)
    finally:
        engine.dispose()

    assert len(service.thread_ids) == 1
    assert service.thread_ids[0] != loop_thread


def test_historical_request_and_response(tmp_path: Path) -> None:
    service = FakeService()
    provider, engine = _provider(_credentials(tmp_path / "historical"), service)
    try:
        ideas = anyio.run(provider.historical_metrics, ["one", "two"], Market.parse("ru", "RU"))
    finally:
        engine.dispose()

    assert [idea.text for idea in ideas] == ["keyword"]
    request = service.calls[0][1]
    assert request["keywords"] == ["one", "two"]
    assert request["language"] == "languageConstants/1031"
    assert request["geo_target_constants"] == ["geoTargetConstants/2643"]


def test_build_service_uses_version_and_complete_client_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from google.ads.googleads.client import GoogleAdsClient  # type: ignore[import-untyped]

    captured: dict[str, object] = {}
    expected_service = object()

    class FakeClient:
        def get_service(self, name: str) -> object:
            captured["service_name"] = name
            return expected_service

    def fake_load(config: dict[str, object], *, version: str | None = None) -> FakeClient:
        captured["config"] = config
        captured["version"] = version
        return FakeClient()

    monkeypatch.setattr(GoogleAdsClient, "load_from_dict", staticmethod(fake_load))
    settings = _credentials(tmp_path / "build-service")
    settings.google_ads_login_customer_id = "9876543210"
    provider = GoogleAdsProvider(settings=settings, cache=None, rate_limiter=None)

    service = provider.build_service()

    assert service is expected_service
    assert captured["version"] == "v25"
    assert captured["service_name"] == "KeywordPlanIdeaService"
    assert captured["config"] == {
        "developer_token": "developer-token",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
        "login_customer_id": "9876543210",
        "use_proto_plus": True,
    }


class CountingRateLimiter(ImmediateRateLimiter):
    def __init__(self, lock_path: Path) -> None:
        super().__init__(lock_path)
        self.acquisitions = 0

    async def acquire(self) -> None:
        self.acquisitions += 1


class PagedService(FakeService):
    """A service whose pager fetches later pages lazily, as the real one does."""

    def __init__(self, pages: int) -> None:
        super().__init__()
        self.pages = pages
        self.page_fetches = 0

    def generate_keyword_ideas(self, *, request: dict[str, Any]) -> Any:
        self.calls.append(("ideas", request))
        self.thread_ids.append(threading.get_ident())

        def page_iterator() -> Any:
            for index in range(self.pages):
                self.page_fetches += 1
                yield SimpleNamespace(
                    results=[_idea(f"keyword-{index}")],
                    next_page_token="more" if index + 1 < self.pages else "",
                )

        return SimpleNamespace(pages=page_iterator())


def test_every_page_after_the_first_is_throttled(tmp_path: Path) -> None:
    """Pagination is more RPCs, and they must respect the per-customer limit.

    generate_keyword_ideas hands back a pager: page one arrives with the initial
    call, every later page is another request issued while the pager is walked.
    Draining it in one go would ignore the one-request-per-second cap that the
    first call was throttled for.
    """
    settings = _credentials(tmp_path / "data")
    engine = open_database(settings)
    service = PagedService(pages=3)
    limiter = CountingRateLimiter(settings.data_dir / "ads.lock")
    provider = GoogleAdsProvider(
        settings=settings,
        cache=SqliteCache(engine, settings),
        rate_limiter=limiter,
        service_factory=lambda: service,
    )
    try:
        page = anyio.run(
            provider.keyword_ideas, AdsSeed(keywords=["keyword"]), Market.parse("en", "US")
        )
    finally:
        engine.dispose()

    assert [idea.text for idea in page.ideas] == ["keyword-0", "keyword-1", "keyword-2"]
    assert service.page_fetches == 3
    # One for the initial call plus one before each of the two extra pages.
    assert limiter.acquisitions == 3


def _run_paged_ideas(
    tmp_path: Path,
    *,
    pages: int,
    max_pages: int,
    calls: int = 1,
) -> tuple[KeywordIdeaPage, PagedService, CountingRateLimiter]:
    settings = _credentials(tmp_path / "data").model_copy(
        update={"google_ads_max_pages": max_pages}
    )
    engine = open_database(settings)
    service = PagedService(pages=pages)
    limiter = CountingRateLimiter(settings.data_dir / "ads.lock")
    provider = GoogleAdsProvider(
        settings=settings,
        cache=SqliteCache(engine, settings),
        rate_limiter=limiter,
        service_factory=lambda: service,
    )
    try:
        page: KeywordIdeaPage | None = None
        for _ in range(calls):
            page = anyio.run(
                provider.keyword_ideas, AdsSeed(keywords=["keyword"]), Market.parse("en", "US")
            )
        assert page is not None
        return page, service, limiter
    finally:
        engine.dispose()


def test_keyword_ideas_fetch_only_max_pages(tmp_path: Path) -> None:
    _page, service, _limiter = _run_paged_ideas(tmp_path, pages=5, max_pages=3)

    assert service.page_fetches == 3


def test_keyword_ideas_truncated_when_pages_exceed_cap(tmp_path: Path) -> None:
    page, _service, _limiter = _run_paged_ideas(tmp_path, pages=5, max_pages=3)

    assert page.truncated is True


def test_keyword_ideas_truncation_reason_names_setting(tmp_path: Path) -> None:
    page, _service, _limiter = _run_paged_ideas(tmp_path, pages=5, max_pages=3)

    assert page.truncation_reason is not None
    assert "google_ads_max_pages" in page.truncation_reason


def test_keyword_ideas_do_not_throttle_unread_pages(tmp_path: Path) -> None:
    _page, _service, limiter = _run_paged_ideas(tmp_path, pages=5, max_pages=3)

    assert limiter.acquisitions == 3


def test_keyword_ideas_include_all_pages_under_cap(tmp_path: Path) -> None:
    page, service, _limiter = _run_paged_ideas(tmp_path, pages=2, max_pages=5)

    assert page.truncated is False
    assert [idea.text for idea in page.ideas] == ["keyword-0", "keyword-1"]
    assert service.page_fetches == 2


def test_last_page_at_the_cap_is_not_truncated(tmp_path: Path) -> None:
    """A pager that ends exactly at the cap is complete, not truncated.

    Nothing here is about the cap firing: the fifth page simply does not
    exist. Checking `next_page_token` first is what makes that distinction,
    and without this case both orders of the two checks pass every other
    test — while the swapped order marks a full answer partial and keeps it
    out of the cache forever.
    """
    page, service, _limiter = _run_paged_ideas(tmp_path, pages=3, max_pages=3)

    assert service.page_fetches == 3
    assert page.truncated is False
    assert page.truncation_reason is None


def test_truncated_keyword_ideas_are_not_cached(tmp_path: Path) -> None:
    _page, service, _limiter = _run_paged_ideas(tmp_path, pages=5, max_pages=3, calls=2)

    assert len(service.calls) == 2


def test_complete_keyword_ideas_remain_cached(tmp_path: Path) -> None:
    _page, service, _limiter = _run_paged_ideas(tmp_path, pages=2, max_pages=5, calls=2)

    assert len(service.calls) == 1


def test_a_month_google_could_not_measure_is_not_a_month_of_no_demand(tmp_path: Path) -> None:
    """`monthly_searches` is optional in Google's schema, and null means unknown.

    Google's own words: "A null value indicates the search volume is unavailable
    for that month." Reading an unset proto field with a plain attribute access
    returns 0, and a zero here is a measurement — nobody searched for this —
    which is the opposite claim, published and then cached for a week. The
    project already refuses this exact confusion on the Trends side, where a
    week marked `hasData: false` still arrives carrying a value of zero.
    """

    class Unset:
        """A proto-plus message whose optional fields are not set."""

        def __init__(self, present: dict[str, object], absent: set[str]) -> None:
            self._present = present
            self._absent = absent
            self._pb = SimpleNamespace(HasField=lambda field: field not in absent)

        def __getattr__(self, field: str) -> object:
            return self._present.get(field, 0)

    metrics = _metrics()
    metrics.monthly_search_volumes = [
        Unset(
            {"year": 2026, "month": SimpleNamespace(name="AUGUST")},
            absent={"monthly_searches"},
        )
    ]
    service = FakeService()
    service.generate_keyword_historical_metrics = (  # type: ignore[method-assign]
        lambda *, request: SimpleNamespace(
            results=[SimpleNamespace(text="keyword", keyword_metrics=metrics)]
        )
    )
    provider, engine = _provider(_credentials(tmp_path / "data"), service)
    try:
        ideas = anyio.run(provider.historical_metrics, ["keyword"], Market.parse("en", "US"))
    finally:
        engine.dispose()

    volume = ideas[0].metrics.monthly_search_volumes[0]
    assert volume.year == 2026
    assert volume.monthly_searches is None, "an unmeasured month must not read as zero searches"


def test_a_missing_client_library_is_a_refusal_rather_than_a_crash(tmp_path: Path) -> None:
    """`is_available()` reads settings, not whether the package is installed.

    The library is an optional extra, so an installation carrying the
    credentials but not the package answers "ready" in `gkai doctor` and then
    raised `ModuleNotFoundError` past every guard on both facades — a traceback
    with empty stdout where the contract promises an envelope.
    """
    settings = _credentials(tmp_path / "data")
    provider, engine = _provider(settings, FakeService())
    provider._service_factory = None
    real_import = builtins.__import__

    def without_google_ads(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("google.ads"):
            raise ModuleNotFoundError("No module named 'google.ads'")
        return real_import(name, *args, **kwargs)

    try:
        with (
            mock.patch.object(builtins, "__import__", without_google_ads),
            pytest.raises(ProviderUnavailableError) as raised,
        ):
            provider.build_service()
    finally:
        engine.dispose()

    assert "not installed" in raised.value.message


@pytest.mark.parametrize(
    ("raised_by_client", "expected", "fragment"),
    [
        pytest.param(
            ValueError("Specified service does not exist in Google Ads API v1."),
            InvalidConfigurationError,
            "could not be built with API version",
            id="unknown_api_version",
        ),
        pytest.param(
            None,
            AuthenticationError,
            "refused the configured credentials",
            id="refused_credentials",
        ),
    ],
)
def test_a_client_that_cannot_be_built_is_a_refusal_rather_than_a_crash(
    tmp_path: Path,
    raised_by_client: BaseException | None,
    expected: type[Exception],
    fragment: str,
) -> None:
    """Building the client is where two non-`GkaiError` failures live.

    `google_ads_api_version` has no validator — the installed client checks it
    and complains with a bare `ValueError`. And credentials are exchanged for a
    token while the client is built, so a wrong client id fails with a
    `RefreshError`. Both escaped every guard on both facades: a typo in one
    environment variable took the whole research run down where a `partial` was
    owed.
    """
    from google.auth.exceptions import RefreshError

    failure = raised_by_client
    if failure is None:
        failure = RefreshError("invalid_client: The OAuth client was not found.")  # type: ignore[no-untyped-call]
    provider, engine = _provider(_credentials(tmp_path / "data"), FakeService())
    provider._service_factory = None
    try:
        with (
            mock.patch(
                "google.ads.googleads.client.GoogleAdsClient.load_from_dict",
                side_effect=failure,
            ),
            pytest.raises(expected) as raised,
        ):
            provider.build_service()
    finally:
        engine.dispose()

    assert fragment in raised.value.message  # type: ignore[attr-defined]


def test_more_seed_keywords_than_google_accepts_is_refused_locally(tmp_path: Path) -> None:
    """The API takes at most twenty, and answers a longer seed with a generic error.

    By then the request has been throttled, sent and charged against the
    account's quota, and what comes back says nothing the caller can act on. The
    research pipeline already batches by twenty; the direct path did not.
    """
    with pytest.raises(InvalidConfigurationError) as raised:
        AdsSeed(keywords=[f"keyword {index}" for index in range(21)]).mode()

    assert "at most 20 seed keywords" in raised.value.message


def test_both_requests_ask_for_the_average_cpc_they_parse(tmp_path: Path) -> None:
    """The value is behind a flag that defaults off.

    The parser read `average_cpc_micros` regardless, so the field was null in
    every live reply while the fakes in these tests filled it in — the one shape
    of dead field the tests cannot catch on their own.
    """
    service = FakeService()
    provider, engine = _provider(_credentials(tmp_path / "data"), service)
    try:
        anyio.run(provider.keyword_ideas, AdsSeed(keywords=["keyword"]), Market.parse("en", "US"))
        anyio.run(provider.historical_metrics, ["keyword"], Market.parse("en", "US"))
    finally:
        engine.dispose()

    for _endpoint, request in service.calls:
        assert request["historical_metrics_options"] == {"include_average_cpc": True}
