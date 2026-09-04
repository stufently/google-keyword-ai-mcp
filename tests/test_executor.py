from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import anyio
import pytest
from sqlalchemy.engine import Engine

from google_keyword_ai import __version__
from google_keyword_ai.cache import PARSER_VERSION
from google_keyword_ai.config import Settings, masked_dump
from google_keyword_ai.market import Market
from google_keyword_ai.pipeline.budget import Budget, BudgetGuard, BudgetSpend
from google_keyword_ai.pipeline.executor import RunExecutor, Stage
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


def _data(*, autocomplete_used: bool = True) -> ResearchData:
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
                SourceUsage(
                    name="autocomplete",
                    used=autocomplete_used,
                    available=autocomplete_used,
                    detail="used" if autocomplete_used else "unavailable",
                ),
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


class FakeScenario:
    def __init__(self, result: ResearchData | None = None, error: Exception | None = None) -> None:
        self.calls = 0
        self.result = _data() if result is None else result
        self.error = error

    async def run(self, context: ScenarioContext) -> ResearchData:
        del context
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class RecordingStore(RunStore):
    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)
        self.events: list[tuple[str, StageStatus]] = []

    def save_stage(self, run_id: str, stage: StageRecord) -> None:
        self.events.append((stage.name, stage.status))
        super().save_stage(run_id, stage)


def _stages() -> list[Stage]:
    return [
        Stage(name="second", position=2, fingerprint_payload={"value": 2}),
        Stage(name="first", position=1, fingerprint_payload={"value": 1}),
    ]


def _record(
    settings: Settings,
    stages: list[Stage],
    *,
    statuses: dict[str, StageStatus] | None = None,
    attempts: int = 0,
    checkpoint: dict[str, object] | None = None,
    app_version: str = __version__,
    parser_version: str = PARSER_VERSION,
) -> RunRecord:
    now = datetime.now(UTC)
    stage_statuses = {} if statuses is None else statuses
    return RunRecord(
        run_id=new_run_id(),
        scenario="niche",
        target="topic",
        language="en",
        country="US",
        status=RunStatus.RUNNING,
        app_version=app_version,
        parser_version=parser_version,
        budget=Budget(),
        config_snapshot=masked_dump(settings),
        created_at=now,
        updated_at=now,
        stages=[
            StageRecord(
                name=stage.name,
                position=stage.position,
                status=stage_statuses.get(stage.name, StageStatus.PENDING),
                fingerprint=stage_fingerprint(stage.name, stage.fingerprint_payload),
                attempts=attempts,
                checkpoint=checkpoint,
            )
            for stage in stages
        ],
    )


def _context(settings: Settings) -> ScenarioContext:
    return ScenarioContext(
        settings=settings,
        market=Market.parse("en", "US"),
        budget_guard=BudgetGuard(Budget()),
        availability={
            "autocomplete": True,
            "google_ads": False,
            "trends": False,
            "search_console": False,
        },
    )


def test_stages_execute_in_position_order(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "order")
    engine = open_database(settings)
    try:
        store = RecordingStore(engine)
        stages = _stages()
        record = _record(settings, stages)
        store.create(record)
        anyio.run(
            partial(
                RunExecutor(store, FakeScenario(), stages).execute,
                record,
                _context(settings),
                resume=False,
            )
        )
        assert store.events == [
            ("first", StageStatus.RUNNING),
            ("first", StageStatus.COMPLETED),
            ("second", StageStatus.RUNNING),
            ("second", StageStatus.COMPLETED),
        ]
    finally:
        engine.dispose()


def test_resume_reuses_valid_checkpoint(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "resume")
    engine = open_database(settings)
    try:
        store = RunStore(engine)
        stages = [Stage(name="expand", position=0, fingerprint_payload={"target": "topic"})]
        checkpoint = _data().model_dump(mode="json")
        record = _record(
            settings,
            stages,
            statuses={"expand": StageStatus.COMPLETED},
            attempts=1,
            checkpoint=checkpoint,
        )
        store.create(record)
        scenario = FakeScenario()
        result = anyio.run(
            partial(
                RunExecutor(store, scenario, stages).execute,
                record,
                _context(settings),
                resume=True,
            )
        )
        assert scenario.calls == 0
        assert result.keywords[0].keyword == "topic one"
        saved = store.get(record.run_id)
        assert saved is not None
        assert saved.stages[0].checkpoint == checkpoint
    finally:
        engine.dispose()


def test_fingerprint_mismatch_runs_stage_again(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "fingerprint")
    engine = open_database(settings)
    try:
        store = RunStore(engine)
        stages = [Stage(name="expand", position=0, fingerprint_payload={"target": "new"})]
        record = _record(
            settings,
            stages,
            statuses={"expand": StageStatus.COMPLETED},
            attempts=1,
            checkpoint=_data().model_dump(mode="json"),
        )
        record.stages[0].fingerprint = "0" * 32
        store.create(record)
        scenario = FakeScenario()
        anyio.run(
            partial(
                RunExecutor(store, scenario, stages).execute,
                record,
                _context(settings),
                resume=True,
            )
        )
        assert scenario.calls == 1
        saved = store.get(record.run_id)
        assert saved is not None
        assert saved.stages[0].attempts == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("field", "value"),
    [("app_version", "old-app"), ("parser_version", "old-parser")],
    ids=["app_version", "parser_version"],
)
def test_app_version_or_parser_version_change_restarts(
    tmp_path: Path, field: str, value: str
) -> None:
    settings = Settings(data_dir=tmp_path / field)
    engine = open_database(settings)
    try:
        store = RunStore(engine)
        stages = [Stage(name="expand", position=0, fingerprint_payload={"target": "topic"})]
        kwargs = {field: value}
        record = _record(
            settings,
            stages,
            statuses={"expand": StageStatus.COMPLETED},
            attempts=1,
            checkpoint=_data().model_dump(mode="json"),
            **kwargs,
        )
        store.create(record)
        scenario = FakeScenario()
        context = _context(settings)
        anyio.run(
            partial(RunExecutor(store, scenario, stages).execute, record, context, resume=True)
        )
        assert scenario.calls == 1
        expected_reason = "Application version" if field == "app_version" else "Parser version"
        # A notice, not a warning: the checkpoints were stale and the scenario
        # ran again, which says nothing about what came back.
        assert any(expected_reason in notice for notice in context.notices)
        assert context.warnings == []
    finally:
        engine.dispose()


def test_failed_stage_is_saved_without_exception(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "failed")
    engine = open_database(settings)
    try:
        store = RunStore(engine)
        stages = [Stage(name="expand", position=0, fingerprint_payload={})]
        record = _record(settings, stages)
        store.create(record)
        result = anyio.run(
            partial(
                RunExecutor(store, FakeScenario(error=RuntimeError("boom")), stages).execute,
                record,
                _context(settings),
                resume=False,
            )
        )
        saved = store.get(record.run_id)
        assert result.keywords == []
        assert saved is not None
        assert saved.status is RunStatus.FAILED
        assert saved.stages[0].status is StageStatus.FAILED
        assert saved.stages[0].error == "boom"
    finally:
        engine.dispose()


def test_attempts_grow_when_failed_stage_is_retried(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "attempts")
    engine = open_database(settings)
    try:
        store = RunStore(engine)
        stages = [Stage(name="expand", position=0, fingerprint_payload={})]
        record = _record(
            settings,
            stages,
            statuses={"expand": StageStatus.FAILED},
            attempts=1,
        )
        store.create(record)
        anyio.run(
            partial(
                RunExecutor(store, FakeScenario(), stages).execute,
                record,
                _context(settings),
                resume=True,
            )
        )
        saved = store.get(record.run_id)
        assert saved is not None
        assert saved.stages[0].attempts == 2
    finally:
        engine.dispose()


def test_skipped_source_stage_has_completed_checkpoint(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "skipped")
    engine = open_database(settings)
    try:
        store = RunStore(engine)
        stages = [Stage(name="expand", position=0, fingerprint_payload={})]
        record = _record(settings, stages)
        store.create(record)
        anyio.run(
            partial(
                RunExecutor(store, FakeScenario(_data(autocomplete_used=False)), stages).execute,
                record,
                _context(settings),
                resume=False,
            )
        )
        saved = store.get(record.run_id)
        assert saved is not None
        assert saved.stages[0].status is StageStatus.COMPLETED
        assert saved.stages[0].checkpoint == {"skipped": True, "reason": "unavailable"}
    finally:
        engine.dispose()


def test_one_stale_stage_replays_the_whole_scenario(tmp_path: Path) -> None:
    """Resume is all-or-nothing, and docs/runs.md says so.

    A scenario is a single coroutine, not a chain of separately invocable
    stages, so the executor can only skip work when *every* stage is reusable.
    With one stale stage it replays the scenario end to end and merely records
    the stale stage; the reused checkpoint saves nothing. Repeat provider calls
    are absorbed by the HTTP cache, not by the run store. This test pins that
    behaviour so the promise in the docs cannot quietly drift away from it.
    """
    settings = Settings(data_dir=tmp_path / "partial-resume")
    engine = open_database(settings)
    try:
        store = RecordingStore(engine)
        stages = [
            Stage(name="first", position=0, fingerprint_payload={"value": 1}),
            Stage(name="second", position=1, fingerprint_payload={"value": 2}),
        ]
        record = _record(
            settings,
            stages,
            statuses={"first": StageStatus.COMPLETED, "second": StageStatus.COMPLETED},
            attempts=1,
            checkpoint=_data().model_dump(mode="json"),
        )
        record.stages[1].fingerprint = "0" * 32
        store.create(record)
        scenario = FakeScenario()
        anyio.run(
            partial(
                RunExecutor(store, scenario, stages).execute,
                record,
                _context(settings),
                resume=True,
            )
        )

        assert scenario.calls == 1
        assert store.events == [
            ("second", StageStatus.RUNNING),
            ("second", StageStatus.COMPLETED),
            # The reused stage is never re-run, but its checkpoint is rewritten
            # so it describes the result the replay just produced.
            ("first", StageStatus.COMPLETED),
        ]
        saved = store.get(record.run_id)
        assert saved is not None
        assert [stage.status for stage in saved.stages] == [
            StageStatus.COMPLETED,
            StageStatus.COMPLETED,
        ]
    finally:
        engine.dispose()


def test_discarded_checkpoints_say_the_request_changed(tmp_path: Path) -> None:
    """Replaying a changed request silently answers a question nobody asked.

    A fingerprint mismatch means the saved work was done for a different
    target, market, budget or seed keyword. Replaying is right, but the caller
    has to be told, because the result no longer matches the request the run
    was created from. Runs saved before schema v3 land here too: their seed
    keyword was never written down, so it cannot be rebuilt.
    """
    settings = Settings(data_dir=tmp_path / "changed-request")
    engine = open_database(settings)
    try:
        store = RecordingStore(engine)
        stages = [Stage(name="first", position=0, fingerprint_payload={"value": 1})]
        record = _record(
            settings,
            stages,
            statuses={"first": StageStatus.COMPLETED},
            attempts=1,
            checkpoint=_data().model_dump(mode="json"),
        )
        record.stages[0].fingerprint = "0" * 32
        store.create(record)
        context = _context(settings)
        anyio.run(
            partial(
                RunExecutor(store, FakeScenario(), stages).execute,
                record,
                context,
                resume=True,
            )
        )

        assert context.notices == [
            "The request changed since this run was saved, so its checkpoints "
            "for first were discarded and the scenario ran again."
        ]
    finally:
        engine.dispose()


def test_replay_refreshes_the_checkpoint_of_a_reused_stage(tmp_path: Path) -> None:
    """The mirror of the test above: the *first* stage is the stale one.

    Refreshing only the replayed stages would leave the later, reused stage
    holding the previous result. The executor picks the last reusable
    checkpoint when nothing needs running, so the very next resume would hand
    back that stale result and silently discard the replay.
    """
    settings = Settings(data_dir=tmp_path / "stale-first")
    engine = open_database(settings)
    try:
        store = RunStore(engine)
        stages = [
            Stage(name="first", position=0, fingerprint_payload={"value": 1}),
            Stage(name="second", position=1, fingerprint_payload={"value": 2}),
        ]
        old_data = _data()
        record = _record(
            settings,
            stages,
            statuses={"first": StageStatus.COMPLETED, "second": StageStatus.COMPLETED},
            attempts=1,
            checkpoint=old_data.model_dump(mode="json"),
        )
        record.stages[0].fingerprint = "0" * 32
        store.create(record)

        fresh = old_data.model_copy(deep=True)
        fresh.keywords[0].keyword = "fresh keyword"
        replayed = anyio.run(
            partial(
                RunExecutor(store, FakeScenario(fresh), stages).execute,
                record,
                _context(settings),
                resume=True,
            )
        )
        assert replayed.keywords[0].keyword == "fresh keyword"

        saved = store.get(record.run_id)
        assert saved is not None
        second_resume = anyio.run(
            partial(
                RunExecutor(store, FakeScenario(), stages).execute,
                saved,
                _context(settings),
                resume=True,
            )
        )
        assert second_resume.keywords[0].keyword == "fresh keyword"
    finally:
        engine.dispose()


def test_a_saved_result_that_cannot_be_read_is_not_a_crash(tmp_path: Path) -> None:
    """A resume that finds a damaged result must answer, not raise.

    Every stage is reusable and every checkpoint records a skipped source, so
    the executor falls back to the run's own stored result. That blob is read
    back with the same `model_validate` the checkpoints use, but without their
    guard: a `ValidationError` is a `ValueError` and no `GkaiError`, so a
    damaged row reaches both facades as a crash rather than as the empty
    envelope every other unreadable stored value produces.
    """
    settings = Settings(data_dir=tmp_path / "damaged-result")
    engine = open_database(settings)
    try:
        store = RunStore(engine)
        stages = [Stage(name="expand", position=0, fingerprint_payload={})]
        record = _record(
            settings,
            stages,
            statuses={"expand": StageStatus.COMPLETED},
            attempts=1,
            checkpoint={"skipped": True, "reason": "unavailable"},
        )
        record.result = {"data": {"scenario": ["not", "a", "name"]}}
        store.create(record)
        context = _context(settings)
        scenario = FakeScenario()
        data = anyio.run(
            partial(
                RunExecutor(store, scenario, stages).execute,
                record,
                context,
                resume=True,
            )
        )
    finally:
        engine.dispose()

    assert scenario.calls == 0, "every stage was reusable, so nothing should have replayed"
    assert data.keywords == []
    assert context.warnings, "a resume that recovered nothing has to say so"
