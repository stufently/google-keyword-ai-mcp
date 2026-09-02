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


def test_all_five_functions_use_saved_run(tmp_path: Path) -> None:
    settings, run_id = saved_run(tmp_path)
    assert run_score(settings, run_id).completeness is Completeness.COMPLETE
    assert run_cluster(settings, run_id).completeness is Completeness.COMPLETE
    assert run_explain_score(settings, run_id, "Alpha Keyword Tool").data.keyword
    assert run_niche_analyze(settings, run_id).data.seed == "alpha"
    assert run_keyword_inspect(settings, run_id, "alpha keyword tool").data.metrics


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
    assert len(result.factors) == 8
    assert any(not factor.available for factor in result.factors)
    available = [factor.value for factor in result.factors if factor.available]
    assert None not in available
    assert result.opportunity_score == pytest.approx(sum(available) / len(available))  # type: ignore[arg-type]
