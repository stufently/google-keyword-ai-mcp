from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio.to_thread
import pytest
from pydantic import SecretStr

from google_keyword_ai.cache import SqliteCache
from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness
from google_keyword_ai.errors import ApiError
from google_keyword_ai.providers.base import ProviderInfo
from google_keyword_ai.providers.google_ads import GoogleAdsProvider, KeywordIdeaPage
from google_keyword_ai.ratelimit import InterProcessRateLimiter
from google_keyword_ai.usecases import ads as ads_usecase
from test_google_ads_provider import PagedService


async def _working_thread_runner[T](function: Callable[..., T], *args: object) -> T:
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(function, *args).result()


class ImmediateRateLimiter(InterProcessRateLimiter):
    def __init__(self, lock_path: Path) -> None:
        super().__init__(1.0, lock_path)

    async def acquire(self) -> None:
        return None


def _metrics() -> SimpleNamespace:
    return SimpleNamespace(
        avg_monthly_searches=100,
        monthly_search_volumes=[],
        competition=SimpleNamespace(name="MEDIUM"),
        competition_index=50,
        low_top_of_page_bid_micros=1_000_000,
        high_top_of_page_bid_micros=2_000_000,
        average_cpc_micros=1_500_000,
    )


class FakeService:
    def __init__(
        self,
        *,
        ideas: list[SimpleNamespace] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.ideas = (
            [SimpleNamespace(text="keyword idea", keyword_idea_metrics=_metrics())]
            if ideas is None
            else ideas
        )
        self.error = error
        self.requests: list[dict[str, Any]] = []

    def generate_keyword_ideas(self, *, request: dict[str, Any]) -> list[SimpleNamespace]:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.ideas

    def generate_keyword_historical_metrics(self, *, request: dict[str, Any]) -> SimpleNamespace:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            results=[
                SimpleNamespace(text=idea.text, keyword_metrics=idea.keyword_idea_metrics)
                for idea in self.ideas
            ]
        )


@pytest.fixture(autouse=True)
def working_thread_offload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(anyio.to_thread, "run_sync", _working_thread_runner)


def _credentials(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        google_ads_developer_token=SecretStr("developer-token"),
        google_ads_customer_id="1234567890",
        google_ads_client_id=SecretStr("client-id"),
        google_ads_client_secret=SecretStr("client-secret"),
        google_ads_refresh_token=SecretStr("refresh-token"),
    )


def _install_service(monkeypatch: pytest.MonkeyPatch, service: object) -> None:
    def build_provider(settings: Settings, cache: SqliteCache) -> GoogleAdsProvider:
        return GoogleAdsProvider(
            settings=settings,
            cache=cache,
            rate_limiter=ImmediateRateLimiter(settings.data_dir / "ads.lock"),
            service_factory=lambda: service,
        )

    monkeypatch.setattr(ads_usecase, "_build_provider", build_provider)


def test_ads_ideas_success_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService()
    _install_service(monkeypatch, service)

    envelope = ads_usecase.run_ads_ideas(
        _credentials(tmp_path / "success"),
        ["seed"],
        language="en",
        country="US",
    )

    assert envelope.completeness is Completeness.COMPLETE
    assert envelope.data.mode == "keyword_seed"
    assert envelope.data.ideas[0].text == "keyword idea"
    assert envelope.to_wire()["data"]["ideas"][0]["metrics"]["ads_competition"] == "MEDIUM"  # type: ignore[index]


def test_ads_ideas_without_credentials_is_empty(tmp_path: Path) -> None:
    envelope = ads_usecase.run_ads_ideas(
        Settings(data_dir=tmp_path / "missing"),
        ["seed"],
    )

    assert envelope.completeness is Completeness.EMPTY
    assert envelope.data.ideas == []
    assert envelope.errors
    assert "credentials" in envelope.completeness_reason.lower()  # type: ignore[union-attr]


def test_provider_error_is_returned_as_empty_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_service(monkeypatch, FakeService(error=ApiError("service failed")))

    envelope = ads_usecase.run_ads_historical(
        _credentials(tmp_path / "error"),
        ["seed"],
    )

    assert envelope.completeness is Completeness.EMPTY
    assert envelope.errors == ["service failed"]
    assert envelope.completeness_reason == "service failed"


@pytest.mark.parametrize(
    ("target", "expected_mode"),
    [
        ("example.com", "site_seed"),
        ("https://example.com/products/widget", "url_seed"),
    ],
)
def test_competitor_selects_seed_mode(
    target: str,
    expected_mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService()
    _install_service(monkeypatch, service)

    envelope = ads_usecase.run_competitor(_credentials(tmp_path / expected_mode), target)

    assert envelope.completeness is Completeness.COMPLETE
    assert envelope.data.mode == expected_mode
    assert expected_mode in service.requests[0]


def test_competitor_with_keyword_uses_combined_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = FakeService()
    _install_service(monkeypatch, service)

    envelope = ads_usecase.run_competitor(
        _credentials(tmp_path / "combined"),
        "https://example.com/page",
        seed_keyword="seed",
    )

    assert envelope.data.mode == "keyword_and_url_seed"
    assert "keyword_and_url_seed" in service.requests[0]


def test_run_ads_ideas_truncated_page_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = PagedService(pages=5)
    settings = _credentials(tmp_path / "truncated").model_copy(update={"google_ads_max_pages": 3})
    _install_service(monkeypatch, service)

    envelope = ads_usecase.run_ads_ideas(settings, ["seed"], language="en", country="US")

    assert envelope.completeness is Completeness.PARTIAL
    assert envelope.completeness_reason
    assert any("google_ads_max_pages" in warning for warning in envelope.warnings)


def test_run_ads_ideas_truncated_empty_page_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(
        settings: Settings,
        cache: SqliteCache,
        seed: object,
        market: object,
        include_adult: bool,
    ) -> tuple[ProviderInfo, KeywordIdeaPage]:
        del settings, cache, seed, market, include_adult
        return (
            ProviderInfo(name="google_ads", official=True, stability="stable"),
            KeywordIdeaPage(
                ideas=[],
                truncated=True,
                truncation_reason=(
                    "Google Ads keyword ideas were truncated after 1 pages "
                    "(google_ads_max_pages); remaining pages were not requested."
                ),
            ),
        )

    monkeypatch.setattr(ads_usecase, "_fetch_ideas", fake_fetch)

    envelope = ads_usecase.run_ads_ideas(
        _credentials(tmp_path / "truncated-empty"),
        ["seed"],
        language="en",
        country="US",
    )

    assert envelope.completeness is Completeness.PARTIAL
    assert envelope.completeness_reason
    assert any("google_ads_max_pages" in warning for warning in envelope.warnings)
