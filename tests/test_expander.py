from collections.abc import Callable, Sequence
from pathlib import Path

import anyio
import httpx
import pytest
import respx
from sqlalchemy.engine import Engine

from google_keyword_ai.cache import SqliteCache
from google_keyword_ai.config import Settings
from google_keyword_ai.errors import GkaiError, InvalidConfigurationError
from google_keyword_ai.expansion import ExpansionStrategy
from google_keyword_ai.market import Market
from google_keyword_ai.normalize import KeywordCandidate
from google_keyword_ai.providers.autocomplete import (
    FALLBACK_ENDPOINT,
    PRIMARY_ENDPOINT,
    AutocompleteProvider,
)
from google_keyword_ai.providers.expander import ExpansionLimits, ExpansionStats, KeywordExpander
from google_keyword_ai.ratelimit import AsyncRateLimiter
from google_keyword_ai.storage.engine import open_database

ResponseFactory = Callable[[httpx.Request], httpx.Response]


def _settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        http_max_attempts=1,
        autocomplete_rate_limit_per_second=1_000_000_000,
    )


async def _run(
    settings: Settings,
    engine: Engine,
    limits: ExpansionLimits,
    strategies: Sequence[ExpansionStrategy],
) -> tuple[list[KeywordCandidate], ExpansionStats]:
    async with httpx.AsyncClient() as client:
        provider = AutocompleteProvider(
            settings=settings,
            client=client,
            cache=SqliteCache(engine, settings),
            rate_limiter=AsyncRateLimiter(settings.autocomplete_rate_limit_per_second),
        )
        keywords, stats = await KeywordExpander(provider, limits).expand(
            "seed",
            Market.parse("en", "US"),
            strategies=strategies,
        )
    return keywords, stats


def _query(request: httpx.Request) -> str:
    return request.url.params["q"]


def _response(suggestions: list[str]) -> httpx.Response:
    return httpx.Response(200, json=["query", suggestions])


def _execute(
    data_dir: Path,
    limits: ExpansionLimits,
    strategies: Sequence[ExpansionStrategy],
    response_factory: ResponseFactory,
) -> tuple[list[KeywordCandidate], ExpansionStats, list[str]]:
    settings = _settings(data_dir)
    engine = open_database(settings)
    requested: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        requested.append(_query(request))
        return response_factory(request)

    try:
        with respx.mock(assert_all_called=False) as router:
            router.get(PRIMARY_ENDPOINT).mock(side_effect=record)
            result = anyio.run(_run, settings, engine, limits, strategies)
    finally:
        engine.dispose()
    return result[0], result[1], requested


def test_fan_visits_all_strategies(data_dir: Path) -> None:
    _, stats, requested = _execute(
        data_dir,
        ExpansionLimits(),
        list(ExpansionStrategy),
        lambda _request: _response([]),
    )

    assert stats.stopped_by is None
    assert {"seed a", "a seed", "seed 0", "how seed"} <= set(requested)


def test_max_queries_stops_before_next_request(data_dir: Path) -> None:
    _, stats, requested = _execute(
        data_dir,
        ExpansionLimits(max_queries=2),
        [ExpansionStrategy.DIGITS],
        lambda _request: _response([]),
    )

    assert stats.stopped_by == "max_queries"
    assert stats.queries_executed == len(requested) == 2


def test_max_results_stops_on_unique_candidates(data_dir: Path) -> None:
    keywords, stats, requested = _execute(
        data_dir,
        ExpansionLimits(max_results=1),
        [ExpansionStrategy.DIGITS],
        lambda _request: _response(["one", "two"]),
    )

    assert stats.stopped_by == "max_results"
    assert len(keywords) == 2
    assert requested == ["seed"]


def test_max_runtime_stops_before_first_request(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    times = iter([10.0, 12.0])
    monkeypatch.setattr(
        "google_keyword_ai.providers.expander.anyio.current_time",
        lambda: next(times),
    )

    _, stats, requested = _execute(
        data_dir,
        ExpansionLimits(max_runtime_seconds=1),
        [ExpansionStrategy.DIGITS],
        lambda _request: _response([]),
    )

    assert stats.stopped_by == "max_runtime"
    assert stats.queries_executed == 0
    assert requested == []


def test_max_depth_stops_when_another_level_is_available(data_dir: Path) -> None:
    def response(request: httpx.Request) -> httpx.Response:
        return _response(["first"] if _query(request) == "seed" else ["second"])

    _, stats, requested = _execute(
        data_dir,
        ExpansionLimits(max_depth=1),
        [],
        response,
    )

    assert stats.stopped_by == "max_depth"
    assert stats.depth_reached == 0
    # max_depth=1 is one round of fan-out, so the keyword found in it is never
    # queried. Asking for "first" here would mean the limit ran a round too many.
    assert requested == ["seed"]


def test_non_seed_failure_is_skipped(data_dir: Path) -> None:
    settings = _settings(data_dir)
    engine = open_database(settings)

    def response(request: httpx.Request) -> httpx.Response:
        if _query(request) == "seed 0":
            return httpx.Response(500)
        return _response(["survivor"] if _query(request) == "seed 1" else [])

    async def run() -> tuple[list[KeywordCandidate], ExpansionStats]:
        async with httpx.AsyncClient() as client:
            provider = AutocompleteProvider(
                settings=settings,
                client=client,
                cache=SqliteCache(engine, settings),
                rate_limiter=AsyncRateLimiter(settings.autocomplete_rate_limit_per_second),
            )
            return await KeywordExpander(provider, ExpansionLimits(max_queries=3)).expand(
                "seed",
                Market.parse("en", "US"),
                strategies=[ExpansionStrategy.DIGITS],
            )

    try:
        with respx.mock(assert_all_called=True) as router:
            router.get(PRIMARY_ENDPOINT).mock(side_effect=response)
            router.get(FALLBACK_ENDPOINT).mock(side_effect=response)
            keywords, stats = anyio.run(run)
    finally:
        engine.dispose()

    assert stats.queries_executed == 3
    assert [keyword.normalized for keyword in keywords] == ["survivor"]
    assert stats.queries_failed == 1, "a skipped request has to be counted, not just survived"


def test_seed_failure_is_raised(data_dir: Path) -> None:
    settings = _settings(data_dir)
    engine = open_database(settings)
    try:
        with respx.mock(assert_all_called=True) as router:
            router.get(PRIMARY_ENDPOINT).mock(return_value=httpx.Response(500))
            router.get(FALLBACK_ENDPOINT).mock(return_value=httpx.Response(500))
            with pytest.raises(GkaiError):
                anyio.run(
                    _run,
                    settings,
                    engine,
                    ExpansionLimits(),
                    [ExpansionStrategy.DIGITS],
                )
    finally:
        engine.dispose()


def test_discovered_sources_include_strategy_query_and_are_merged(data_dir: Path) -> None:
    def response(request: httpx.Request) -> httpx.Response:
        query = _query(request)
        return _response(["Same Keyword"] if query in {"seed 0", "seed 1"} else [])

    keywords, _, _ = _execute(
        data_dir,
        ExpansionLimits(max_queries=3),
        [ExpansionStrategy.DIGITS],
        response,
    )

    assert len(keywords) == 1
    keyword = keywords[0]
    assert keyword.discovered_from == [
        "autocomplete:digits:seed 0",
        "autocomplete:digits:seed 1",
    ]


def test_depth_two_uses_keywords_found_at_previous_depth(data_dir: Path) -> None:
    def response(request: httpx.Request) -> httpx.Response:
        suggestions = {"seed": ["first"], "first": ["second"], "second": []}
        return _response(suggestions[_query(request)])

    _, stats, requested = _execute(
        data_dir,
        ExpansionLimits(max_depth=2),
        [],
        response,
    )

    # Two rounds: the seed, then the keyword it produced. "second" belongs to a
    # third round that was not requested.
    assert stats.depth_reached == 1
    assert stats.stopped_by == "max_depth"
    assert requested == ["seed", "first"]


def test_expansion_that_runs_out_of_new_keywords_reports_no_stop_reason(
    data_dir: Path,
) -> None:
    def response(request: httpx.Request) -> httpx.Response:
        suggestions = {"seed": ["first"], "first": ["second"], "second": []}
        return _response(suggestions[_query(request)])

    _, stats, requested = _execute(
        data_dir,
        ExpansionLimits(max_depth=5),
        [],
        response,
    )

    assert stats.stopped_by is None
    assert stats.depth_reached == 2
    assert requested == ["seed", "first", "second"]


@pytest.mark.parametrize(
    "values",
    [
        {"max_depth": 0},
        {"max_queries": 0},
        {"max_results": 0},
        {"max_runtime_seconds": 0},
    ],
)
def test_limits_must_be_positive(values: dict[str, int]) -> None:
    with pytest.raises(InvalidConfigurationError):
        ExpansionLimits(**values)


def test_result_holds_each_keyword_once_when_the_fan_out_finishes_normally(
    data_dir: Path,
) -> None:
    """Deduplication must survive the path that does not hit a budget guard.

    The guard-return path deduplicates on its way out, so a test that stops on
    `max_queries` keeps passing even if the per-round deduplication is removed.
    This one lets the round finish so the returned list is the one built up
    across queries.
    """

    def response(request: httpx.Request) -> httpx.Response:
        query = _query(request)
        return _response(["Same Keyword"] if query in {"seed 0", "seed 1", "seed 2"} else [])

    keywords, stats, _ = _execute(
        data_dir,
        ExpansionLimits(),
        [ExpansionStrategy.DIGITS],
        response,
    )

    assert stats.stopped_by == "max_depth"
    normalized = [keyword.normalized for keyword in keywords]
    assert normalized == ["same keyword"]
    assert len(normalized) == len(set(normalized))
