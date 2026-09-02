from collections.abc import Sequence
from pathlib import Path

import pytest

from google_keyword_ai.cache import SqliteCache
from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness
from google_keyword_ai.errors import NetworkError
from google_keyword_ai.expansion import ExpansionStrategy
from google_keyword_ai.market import Market
from google_keyword_ai.normalize import KeywordCandidate
from google_keyword_ai.providers.base import ProviderInfo
from google_keyword_ai.providers.expander import ExpansionLimits, ExpansionStats
from google_keyword_ai.usecases import expand as expand_usecase


def _candidate(name: str) -> KeywordCandidate:
    return KeywordCandidate(
        raw=name,
        normalized=name,
        discovered_from=["autocomplete:seed:seed"],
    )


def _stub_result(
    keywords: list[KeywordCandidate],
    stats: ExpansionStats,
) -> object:
    async def fetch(
        _settings: Settings,
        _cache: SqliteCache,
        _seed: str,
        _market: Market,
        _limits: ExpansionLimits,
        _strategies: Sequence[ExpansionStrategy],
    ) -> tuple[ProviderInfo, list[KeywordCandidate], ExpansionStats]:
        return (
            ProviderInfo(name="autocomplete", official=False, stability="unofficial"),
            keywords,
            stats,
        )

    return fetch


def test_success_envelope(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        expand_usecase,
        "_fetch_expansion",
        _stub_result([_candidate("one")], ExpansionStats(queries_executed=4, depth_reached=0)),
    )

    envelope = expand_usecase.run_expand(Settings(data_dir=data_dir), "seed")

    assert envelope.completeness is Completeness.COMPLETE
    assert [keyword.normalized for keyword in envelope.data.keywords] == ["one"]
    assert envelope.data.strategies == [strategy.value for strategy in ExpansionStrategy]


def test_partial_when_safeguard_stops(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        expand_usecase,
        "_fetch_expansion",
        _stub_result(
            [_candidate("one")],
            ExpansionStats(queries_executed=2, depth_reached=0, stopped_by="max_queries"),
        ),
    )

    envelope = expand_usecase.run_expand(Settings(data_dir=data_dir), "seed")

    assert envelope.completeness is Completeness.PARTIAL
    assert envelope.completeness_reason == "stopped by max_queries"


@pytest.mark.parametrize("budget_stop", ["max_queries", "max_results", "max_runtime"])
def test_every_budget_guard_reports_partial(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, budget_stop: str
) -> None:
    monkeypatch.setattr(
        expand_usecase,
        "_fetch_expansion",
        _stub_result(
            [_candidate("one")],
            ExpansionStats(queries_executed=2, depth_reached=0, stopped_by=budget_stop),
        ),
    )

    envelope = expand_usecase.run_expand(Settings(data_dir=data_dir), "seed")

    assert envelope.completeness is Completeness.PARTIAL
    assert envelope.completeness_reason == f"stopped by {budget_stop}"


def test_reaching_the_requested_depth_is_not_a_partial_result(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finishing the scope the caller asked for is a complete answer.

    Running out of budget cuts an answer short; reaching the configured depth
    does not. Marking it partial would make every ordinary expansion report
    partial and exit non-zero, because there is always another level below.
    """
    monkeypatch.setattr(
        expand_usecase,
        "_fetch_expansion",
        _stub_result(
            [_candidate("one")],
            ExpansionStats(queries_executed=9, depth_reached=0, stopped_by="max_depth"),
        ),
    )

    envelope = expand_usecase.run_expand(Settings(data_dir=data_dir), "seed")

    assert envelope.completeness is Completeness.COMPLETE
    assert envelope.data.stats.stopped_by == "max_depth"


def test_provider_failure_returns_empty(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(
        _settings: Settings,
        _cache: SqliteCache,
        _seed: str,
        _market: Market,
        _limits: ExpansionLimits,
        _strategies: Sequence[ExpansionStrategy],
    ) -> tuple[ProviderInfo, list[KeywordCandidate], ExpansionStats]:
        raise NetworkError("offline")

    monkeypatch.setattr(expand_usecase, "_fetch_expansion", fail)

    envelope = expand_usecase.run_expand(Settings(data_dir=data_dir), "seed")

    assert envelope.completeness is Completeness.EMPTY
    assert envelope.data.keywords == []
    assert envelope.errors == ["offline"]
    assert envelope.completeness_reason == "offline"


def test_limit_only_slices_keywords_not_stats(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stats = ExpansionStats(queries_executed=27, depth_reached=1)
    monkeypatch.setattr(
        expand_usecase,
        "_fetch_expansion",
        _stub_result([_candidate("one"), _candidate("two")], stats),
    )

    envelope = expand_usecase.run_expand(Settings(data_dir=data_dir), "seed", limit=1)

    assert [keyword.normalized for keyword in envelope.data.keywords] == ["one"]
    assert envelope.data.stats == stats


def test_failed_requests_make_the_result_partial_and_say_how_many(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipping a dead request keeps the fan-out alive; hiding it invents a small niche.

    Nothing about the returned keywords distinguishes a seed with few
    variations from a source that failed eighty times, so a result built on
    skipped requests must say so rather than report itself whole.
    """
    monkeypatch.setattr(
        expand_usecase,
        "_fetch_expansion",
        _stub_result(
            [_candidate("one")],
            ExpansionStats(queries_executed=88, depth_reached=0, queries_failed=80),
        ),
    )

    envelope = expand_usecase.run_expand(Settings(data_dir=data_dir), "seed")

    assert envelope.completeness is Completeness.PARTIAL
    assert envelope.completeness_reason is not None
    assert "80 of 88" in envelope.completeness_reason
    assert envelope.warnings == [envelope.completeness_reason]


def test_a_budget_stop_keeps_its_reason_and_still_reports_the_failures(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different things went wrong, and the reader needs both.

    The budget stop is why collection ended; the failures are why what was
    collected is thinner than the budget would suggest.
    """
    monkeypatch.setattr(
        expand_usecase,
        "_fetch_expansion",
        _stub_result(
            [_candidate("one")],
            ExpansionStats(
                queries_executed=10,
                depth_reached=0,
                stopped_by="max_queries",
                queries_failed=3,
            ),
        ),
    )

    envelope = expand_usecase.run_expand(Settings(data_dir=data_dir), "seed")

    assert envelope.completeness is Completeness.PARTIAL
    assert envelope.completeness_reason == "stopped by max_queries"
    assert "3 of 10" in envelope.warnings[0]


def test_an_empty_result_built_on_failures_says_so_instead_of_no_keywords(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty answer is exactly where the difference matters most.

    "no keywords" reads as an empty niche. When every request failed, the niche
    was never measured at all, and the caller who cannot tell those apart draws
    the opposite conclusion from the one the data supports.
    """
    monkeypatch.setattr(
        expand_usecase,
        "_fetch_expansion",
        _stub_result(
            [],
            ExpansionStats(queries_executed=88, depth_reached=0, queries_failed=80),
        ),
    )

    envelope = expand_usecase.run_expand(Settings(data_dir=data_dir), "seed")

    assert envelope.completeness is Completeness.EMPTY
    assert envelope.completeness_reason is not None
    assert "80 of 88" in envelope.completeness_reason
    assert envelope.warnings == [envelope.completeness_reason]


def test_an_empty_result_without_failures_still_says_no_keywords(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plain empty answer must not be dressed up as a failure either."""
    monkeypatch.setattr(
        expand_usecase,
        "_fetch_expansion",
        _stub_result([], ExpansionStats(queries_executed=88, depth_reached=0)),
    )

    envelope = expand_usecase.run_expand(Settings(data_dir=data_dir), "seed")

    assert envelope.completeness is Completeness.EMPTY
    assert envelope.completeness_reason == "no keywords"
    assert envelope.warnings == []
