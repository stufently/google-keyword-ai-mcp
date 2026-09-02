import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from google_keyword_ai import __version__
from google_keyword_ai.cache import PARSER_VERSION
from google_keyword_ai.config import Settings, masked_dump
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.market import Market
from google_keyword_ai.pipeline.budget import Budget, BudgetGuard, BudgetSpend
from google_keyword_ai.pipeline.executor import scenario_stages
from google_keyword_ai.pipeline.models import (
    DataQuality,
    ResearchData,
    ResearchKeyword,
    ResearchStats,
    SourceUsage,
)
from google_keyword_ai.pipeline.runs import (
    RunRecord,
    RunStatus,
    RunStore,
    StageRecord,
    StageStatus,
    new_run_id,
    stage_fingerprint,
)
from google_keyword_ai.pipeline.scenarios import ScenarioContext
from google_keyword_ai.storage.engine import open_database
from google_keyword_ai.usecases import research as research_module
from google_keyword_ai.usecases import runs as runs_module
from google_keyword_ai.usecases.research import run_research
from google_keyword_ai.usecases.runs import run_export, run_rerun, run_resume, run_show


def _data() -> ResearchData:
    return ResearchData(
        scenario="niche",
        input="topic",
        language="en",
        country="US",
        keywords=[
            ResearchKeyword(
                keyword="topic one",
                normalized="topic one",
                discovered_from=["autocomplete"],
            )
        ],
        stats=ResearchStats(spend=BudgetSpend()),
        data_quality=DataQuality(
            sources=[
                SourceUsage(name="autocomplete", used=True, available=True, detail="used"),
                SourceUsage(name="google_ads", used=False, available=False, detail="unavailable"),
                SourceUsage(name="trends", used=False, available=False, detail="unavailable"),
                SourceUsage(
                    name="search_console", used=False, available=False, detail="unavailable"
                ),
            ],
            retrieved_at=datetime.now(UTC),
            absolute_metrics=[],
            relative_metrics=[],
            derived_metrics=[],
            caveats=[],
        ),
    )


def _context(settings: Settings, market: Market, budget: Budget) -> ScenarioContext:
    return ScenarioContext(
        settings=settings,
        market=market,
        budget_guard=BudgetGuard(budget),
        availability={
            "autocomplete": False,
            "google_ads": False,
            "trends": False,
            "search_console": False,
        },
    )


@asynccontextmanager
async def _fake_live_context(
    settings: Settings,
    market: Market,
    budget: Budget,
    cache: object,
) -> AsyncIterator[ScenarioContext]:
    del cache
    yield _context(settings, market, budget)


def _completed_record(settings: Settings) -> RunRecord:
    market = Market.parse("en", "US")
    budget = Budget()
    stages = scenario_stages("niche", target="topic", market=market, budget=budget)
    data = _data()
    result = Envelope(data=data).to_wire()
    now = datetime.now(UTC)
    return RunRecord(
        run_id=new_run_id(),
        scenario="niche",
        target="topic",
        language="en",
        country="US",
        status=RunStatus.COMPLETED,
        app_version=__version__,
        parser_version=PARSER_VERSION,
        budget=budget,
        config_snapshot=masked_dump(settings),
        result=result,
        created_at=now,
        updated_at=now,
        stages=[
            StageRecord(
                name=stage.name,
                position=stage.position,
                status=StageStatus.COMPLETED,
                fingerprint=stage_fingerprint(stage.name, stage.fingerprint_payload),
                attempts=1,
                checkpoint=data.model_dump(mode="json"),
                started_at=now,
                finished_at=now,
            )
            for stage in stages
        ],
    )


def test_show_and_export_missing_run_are_empty(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "missing")
    shown = run_show(settings, "run_missing")
    exported = run_export(settings, "run_missing")
    assert shown.completeness is Completeness.EMPTY
    assert shown.completeness_reason
    assert exported.completeness is Completeness.EMPTY
    assert exported.completeness_reason


def test_resume_does_not_replay_valid_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "resume")
    engine = open_database(settings)
    try:
        record = _completed_record(settings)
        RunStore(engine).create(record)
    finally:
        engine.dispose()

    class NeverRunScenario:
        async def run(self, context: ScenarioContext) -> ResearchData:
            del context
            raise AssertionError("valid stages must not be replayed")

    monkeypatch.setattr(runs_module, "_live_context", _fake_live_context)
    monkeypatch.setattr(
        runs_module,
        "_scenario_for_name",
        lambda name, target, seed: NeverRunScenario(),
    )
    resumed = run_resume(settings, record.run_id)
    assert resumed.run_id == record.run_id
    assert resumed.data.keywords[0].keyword == "topic one"


def test_rerun_creates_new_run_id_without_touching_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "rerun")
    engine = open_database(settings)
    try:
        original = _completed_record(settings)
        store = RunStore(engine)
        store.create(original)
        before = store.get(original.run_id)
    finally:
        engine.dispose()

    monkeypatch.setattr(research_module, "_live_context", _fake_live_context)
    rerun = run_rerun(settings, original.run_id)

    assert rerun.run_id is not None
    assert rerun.run_id != original.run_id
    engine = open_database(settings)
    try:
        store = RunStore(engine)
        assert store.get(original.run_id) == before
        assert store.get(rerun.run_id) is not None
    finally:
        engine.dispose()


def test_research_save_run_sets_id_and_without_save_run_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "save-run")

    async def fake_execute(*args: object) -> tuple[ResearchData, list[str], list[str]]:
        del args
        return _data(), [], []

    monkeypatch.setattr(research_module, "_execute", fake_execute)
    unsaved = run_research(settings, "topic", scenario="niche")
    assert unsaved.run_id is None
    engine = open_database(settings)
    try:
        assert RunStore(engine).list() == []
    finally:
        engine.dispose()

    monkeypatch.setattr(research_module, "_live_context", _fake_live_context)
    saved = run_research(settings, "topic", scenario="niche", save_run=True)
    assert saved.run_id is not None
    engine = open_database(settings)
    try:
        records = RunStore(engine).list()
        assert [record.run_id for record in records] == [saved.run_id]
    finally:
        engine.dispose()


def test_saved_run_never_stores_a_secret_from_the_real_research_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check the bytes the production path wrote, not a helper's own dump.

    A test that builds the snapshot itself only re-tests the masking helper: it
    stays green even if the use-case persists raw settings. Run the real
    save-run path with a secret in the environment and read the column back.
    """
    secret = "token-that-must-never-be-persisted"
    monkeypatch.setenv("GKAI_GOOGLE_ADS_DEVELOPER_TOKEN", secret)
    settings = Settings(data_dir=tmp_path / "secret-run")

    async def fake_execute(*args: object) -> tuple[ResearchData, list[str], list[str]]:
        del args
        return _data(), [], []

    monkeypatch.setattr(research_module, "_execute", fake_execute)
    monkeypatch.setattr(research_module, "_live_context", _fake_live_context)

    envelope = run_research(settings, "topic", scenario="niche", save_run=True)

    engine = open_database(settings)
    try:
        with engine.connect() as connection:
            stored = connection.exec_driver_sql(
                "SELECT config_snapshot FROM runs WHERE run_id = ?", (envelope.run_id,)
            ).scalar_one()
    finally:
        engine.dispose()

    assert secret not in stored
    assert json.loads(stored)["google_ads_developer_token"] == "***"
