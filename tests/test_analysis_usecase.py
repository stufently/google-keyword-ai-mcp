from datetime import UTC, datetime
from pathlib import Path

import pytest

from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.pipeline.budget import Budget, BudgetSpend
from google_keyword_ai.pipeline.models import (
    DataQuality,
    ResearchData,
    ResearchKeyword,
    ResearchStats,
    SourceUsage,
)
from google_keyword_ai.pipeline.runs import RunRecord, RunStatus, RunStore
from google_keyword_ai.storage.engine import open_database
from google_keyword_ai.usecases.analysis import (
    run_cluster,
    run_explain_score,
    run_keyword_inspect,
    run_niche_analyze,
    run_score,
)


def saved_run(tmp_path: Path) -> tuple[Settings, str]:
    settings = Settings(data_dir=tmp_path)
    now = datetime.now(UTC)
    data = ResearchData(
        scenario="topic",
        input="alpha",
        language="en",
        country="US",
        keywords=[
            ResearchKeyword(
                keyword="alpha keyword tool",
                normalized="alpha keyword tool",
                discovered_from=["autocomplete", "google_ads"],
                autocomplete_relevance=10,
                avg_monthly_searches=1_000,
                high_top_of_page_bid=2.5,
            ),
            ResearchKeyword(
                keyword="alpha keyword software",
                normalized="alpha keyword software",
                discovered_from=["autocomplete"],
            ),
        ],
        stats=ResearchStats(spend=BudgetSpend()),
        data_quality=DataQuality(
            sources=[SourceUsage(name="autocomplete", used=True, available=True, detail="used")],
            retrieved_at=now,
            absolute_metrics=["avg_monthly_searches"],
            relative_metrics=[],
            derived_metrics=[],
            caveats=["No values were imputed."],
        ),
    )
    run_id = "run_analysis_test"
    record = RunRecord(
        run_id=run_id,
        scenario="topic",
        target="alpha",
        language="en",
        country="US",
        status=RunStatus.COMPLETED,
        app_version="test",
        parser_version="test",
        budget=Budget(),
        config_snapshot={},
        result=Envelope(data=data, run_id=run_id).to_wire(),
        created_at=now,
        updated_at=now,
        stages=[],
    )
    engine = open_database(settings)
    try:
        RunStore(engine).create(record)
    finally:
        engine.dispose()
    return settings, run_id


def _run_with_keywords(tmp_path: Path, texts: list[str]) -> tuple[Settings, str]:
    """Save a run holding exactly these keywords, with no metrics on them."""
    settings = Settings(data_dir=tmp_path)
    now = datetime.now(UTC)
    data = ResearchData(
        scenario="topic",
        input="alpha",
        language="en",
        country="US",
        keywords=[
            ResearchKeyword(keyword=text, normalized=text, discovered_from=["autocomplete"])
            for text in texts
        ],
        stats=ResearchStats(spend=BudgetSpend()),
        data_quality=DataQuality(
            sources=[SourceUsage(name="autocomplete", used=True, available=True, detail="used")],
            retrieved_at=now,
            absolute_metrics=[],
            relative_metrics=[],
            derived_metrics=[],
            caveats=[],
        ),
    )
    run_id = "run_keywords"
    record = RunRecord(
        run_id=run_id,
        scenario="topic",
        target="alpha",
        language="en",
        country="US",
        status=RunStatus.COMPLETED,
        app_version="test",
        parser_version="test",
        budget=Budget(),
        config_snapshot={},
        result=Envelope(data=data, run_id=run_id).to_wire(),
        created_at=now,
        updated_at=now,
        stages=[],
    )
    engine = open_database(settings)
    try:
        RunStore(engine).create(record)
    finally:
        engine.dispose()
    return settings, run_id


def _run_with_volumes(tmp_path: Path, volumes: list[int]) -> tuple[Settings, str]:
    """Save a run whose keywords carry exactly these monthly search volumes."""
    settings = Settings(data_dir=tmp_path)
    now = datetime.now(UTC)
    data = ResearchData(
        scenario="topic",
        input="alpha",
        language="en",
        country="US",
        keywords=[
            ResearchKeyword(
                keyword=f"keyword {index}",
                normalized=f"keyword {index}",
                discovered_from=["google_ads"],
                avg_monthly_searches=volume,
            )
            for index, volume in enumerate(volumes)
        ],
        stats=ResearchStats(spend=BudgetSpend()),
        data_quality=DataQuality(
            sources=[SourceUsage(name="google_ads", used=True, available=True, detail="used")],
            retrieved_at=now,
            absolute_metrics=["avg_monthly_searches"],
            relative_metrics=[],
            derived_metrics=[],
            caveats=[],
        ),
    )
    run_id = f"run_volumes_{len(volumes)}"
    record = RunRecord(
        run_id=run_id,
        scenario="topic",
        target="alpha",
        language="en",
        country="US",
        status=RunStatus.COMPLETED,
        app_version="test",
        parser_version="test",
        budget=Budget(),
        config_snapshot={},
        result=Envelope(data=data, run_id=run_id).to_wire(),
        created_at=now,
        updated_at=now,
        stages=[],
    )
    engine = open_database(settings)
    try:
        RunStore(engine).create(record)
    finally:
        engine.dispose()
    return settings, run_id


def test_all_five_functions_use_saved_run(tmp_path: Path) -> None:
    settings, run_id = saved_run(tmp_path)
    assert run_score(settings, run_id).completeness is Completeness.COMPLETE
    assert run_cluster(settings, run_id).completeness is Completeness.COMPLETE
    explained = run_explain_score(settings, run_id, "Alpha Keyword Tool").data
    assert explained is not None and explained.keyword
    niche = run_niche_analyze(settings, run_id).data
    assert niche is not None and niche.seed == "alpha"
    inspected = run_keyword_inspect(settings, run_id, "alpha keyword tool").data
    assert inspected is not None and inspected.metrics


def test_missing_run_is_empty_with_reason(tmp_path: Path) -> None:
    result = run_score(Settings(data_dir=tmp_path), "run_missing")
    assert result.completeness is Completeness.EMPTY
    assert result.completeness_reason


@pytest.mark.parametrize("function", [run_explain_score, run_keyword_inspect])
def test_missing_keyword_is_empty_with_reason(tmp_path: Path, function: object) -> None:
    settings, run_id = saved_run(tmp_path)
    result = function(settings, run_id, "missing")  # type: ignore[operator]
    assert result.completeness is Completeness.EMPTY
    assert result.completeness_reason


def test_niche_always_has_factor_breakdown_and_excludes_unavailable(tmp_path: Path) -> None:
    settings, run_id = saved_run(tmp_path)
    result = run_niche_analyze(settings, run_id).data
    assert result is not None
    assert len(result.factors) == 8
    assert any(not factor.available for factor in result.factors)
    available = [factor.value for factor in result.factors if factor.available]
    assert None not in available
    assert result.opportunity_score == pytest.approx(sum(available) / len(available))  # type: ignore[arg-type]


def test_a_relative_metric_is_marked_as_not_directly_counted(tmp_path: Path) -> None:
    """The flag used to test a list that never holds a metric name at all.

    A run classifies its own figures into absolute, relative and derived, and
    `derived_metrics` carries the single word "opportunities" — never a field of
    a keyword. So `is_derived` compared field names against a list that could
    not contain one, and came back false for every metric on every run,
    including the click-through rate and the position, which are not counts of
    anything.
    """
    settings, run_id = saved_run(tmp_path)

    envelope = run_keyword_inspect(settings, run_id, "alpha keyword tool")

    assert envelope.data is not None
    by_metric = {item.metric: item for item in envelope.data.metrics}
    assert by_metric["avg_monthly_searches"].is_derived is False, "a search count is a count"
    assert by_metric["autocomplete_relevance"].is_derived is True, (
        "a relevance score is not a count of anything"
    )
    assert by_metric["high_top_of_page_bid"].is_derived is True, (
        "this run never recorded bids as an absolute measurement"
    )


@pytest.mark.parametrize(
    ("volumes", "expected"),
    [
        pytest.param([1000] * 20, 75.0, id="twenty_even_keywords_have_a_long_tail"),
        pytest.param([20000], None, id="one_head_term_has_no_tail_to_measure"),
        pytest.param([100] * 5, None, id="five_keywords_are_all_head"),
    ],
)
def test_the_long_tail_share_rewards_the_tail_rather_than_the_head(
    tmp_path: Path, volumes: list[int], expected: float | None
) -> None:
    """Every factor in this score reads "higher is better", so this one must too.

    Measured as concentration it rewarded the opposite: a niche whose entire
    demand sits in one head term scored a full 100 while a twenty-keyword tail
    of identical total demand scored 25 — seventy-five points of opportunity
    handed to the niche with no tail at all. And with five keywords or fewer the
    top five are all of them, so the ratio is identically 1 and measures nothing;
    that is unavailable, not a perfect score.
    """
    settings, run_id = _run_with_volumes(tmp_path, volumes)

    envelope = run_niche_analyze(settings, run_id)

    assert envelope.data is not None
    factor = next(f for f in envelope.data.factors if f.name == "long_tail_demand_share")
    if expected is None:
        assert factor.available is False
        assert factor.value is None
    else:
        assert factor.value == pytest.approx(expected)


def test_the_niche_cluster_count_leaves_out_the_remainder(tmp_path: Path) -> None:
    """`clusters` and `cluster_diversity` have to count the same thing.

    The diversity factor already excluded the leftovers bucket, so counting it
    in `clusters` put two answers to one question into the same response.
    """
    settings = Settings(data_dir=tmp_path, cluster_similarity_threshold=0.3, cluster_min_size=2)
    _, run_id = _run_with_keywords(tmp_path, ["red shoe", "red shoes", "isolated"])

    envelope = run_niche_analyze(settings, run_id)

    assert envelope.data is not None
    assert envelope.data.keywords_analyzed == 3
    assert envelope.data.clusters == 1, "one cluster formed; the leftover is not a second"
    diversity = next(f for f in envelope.data.factors if f.name == "cluster_diversity")
    assert diversity.value == pytest.approx(10.0), "ten percent of the ten-cluster reference"
