from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from google_keyword_ai import __version__
from google_keyword_ai.cache import PARSER_VERSION
from google_keyword_ai.market import Market
from google_keyword_ai.pipeline.budget import Budget, BudgetSpend
from google_keyword_ai.pipeline.models import DataQuality, ResearchData, ResearchStats, SourceUsage
from google_keyword_ai.pipeline.runs import (
    RunRecord,
    RunStatus,
    RunStore,
    StageRecord,
    StageStatus,
    stage_fingerprint,
)
from google_keyword_ai.pipeline.scenarios import GENERAL_CAVEAT, ScenarioContext


class ScenarioLike(Protocol):
    async def run(self, context: ScenarioContext) -> ResearchData: ...


class Stage(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    position: int
    fingerprint_payload: dict[str, object]


_STAGE_NAMES = {
    "niche": ("expand", "ads_metrics", "trends"),
    "competitor": ("ads_ideas", "expand", "trends"),
    "site": ("gsc_query", "opportunities", "ads_metrics", "trends"),
}


def scenario_stages(
    scenario_name: str,
    *,
    target: str,
    market: Market,
    budget: Budget,
    seed_keyword: str | None = None,
) -> list[Stage]:
    try:
        names = _STAGE_NAMES[scenario_name]
    except KeyError as exc:
        raise ValueError(f"Unknown research scenario: {scenario_name}.") from exc
    return [
        Stage(
            name=name,
            position=position,
            fingerprint_payload={
                "name": name,
                "target": target,
                "language": market.language,
                "country": market.country,
                "budget": budget.model_dump(mode="json"),
                "seed_keyword": seed_keyword,
            },
        )
        for position, name in enumerate(names)
    ]


def _expected_fingerprint(stage: Stage) -> str:
    return stage_fingerprint(stage.name, stage.fingerprint_payload)


def _source_for_stage(name: str) -> str | None:
    return {
        "expand": "autocomplete",
        "ads_metrics": "google_ads",
        "ads_ideas": "google_ads",
        "gsc_query": "search_console",
        "trends": "trends",
    }.get(name)


def _stage_checkpoint(stage: Stage, data: ResearchData) -> dict[str, object]:
    source_name = _source_for_stage(stage.name)
    source = next(
        (usage for usage in data.data_quality.sources if usage.name == source_name),
        None,
    )
    if stage.name == "opportunities":
        gsc = next(
            (usage for usage in data.data_quality.sources if usage.name == "search_console"),
            None,
        )
        if gsc is None or not gsc.used:
            return {
                "skipped": True,
                "reason": "Search Console was not used, so opportunities were unavailable.",
            }
    elif source_name is not None and (source is None or not source.used):
        reason = source.detail if source is not None else f"{source_name} was unavailable"
        return {
            "skipped": True,
            "reason": reason,
        }
    return {"research_data": data.model_dump(mode="json")}


def _checkpoint_data(checkpoint: Mapping[str, object]) -> ResearchData | None:
    raw = checkpoint.get("research_data", checkpoint)
    if not isinstance(raw, Mapping):
        return None
    try:
        return ResearchData.model_validate(raw)
    except ValidationError:
        return None


def _record_result_data(record: RunRecord) -> ResearchData | None:
    if record.result is None:
        return None
    raw = record.result.get("data")
    if not isinstance(raw, dict):
        return None
    return ResearchData.model_validate(raw)


def _failed_data(record: RunRecord, context: ScenarioContext) -> ResearchData:
    sources = [
        SourceUsage(
            name=name,
            used=False,
            available=context.available(name),
            detail="execution failed",
        )
        for name in ("autocomplete", "google_ads", "trends", "search_console")
    ]
    return ResearchData(
        scenario=record.scenario,
        input=record.target,
        language=record.language,
        country=record.country,
        keywords=[],
        stats=ResearchStats(spend=BudgetSpend()),
        data_quality=DataQuality(
            sources=sources,
            retrieved_at=datetime.now(UTC),
            absolute_metrics=[],
            relative_metrics=[],
            derived_metrics=[],
            caveats=[GENERAL_CAVEAT],
        ),
    )


class RunExecutor:
    def __init__(
        self,
        store: RunStore,
        scenario: ScenarioLike,
        stages: list[Stage],
    ) -> None:
        self._store = store
        self._scenario = scenario
        self._stages = sorted(stages, key=lambda stage: stage.position)

    async def execute(
        self,
        record: RunRecord,
        context: ScenarioContext,
        *,
        resume: bool,
    ) -> ResearchData:
        version_reason: str | None = None
        if record.app_version != __version__:
            version_reason = (
                f"Application version changed from {record.app_version} to {__version__}; "
                "all stage checkpoints are stale."
            )
        elif record.parser_version != PARSER_VERSION:
            version_reason = (
                f"Parser version changed from {record.parser_version} to {PARSER_VERSION}; "
                "all stage checkpoints are stale."
            )
        if resume and version_reason is not None:
            context.warnings.append(version_reason)

        saved_by_name = {stage.name: stage for stage in record.stages}
        runnable: list[tuple[Stage, StageRecord]] = []
        last_checkpoint_data: ResearchData | None = None
        for stage in self._stages:
            saved = saved_by_name.get(stage.name)
            expected = _expected_fingerprint(stage)
            reusable = (
                resume
                and version_reason is None
                and saved is not None
                and saved.status is StageStatus.COMPLETED
                and saved.fingerprint == expected
                and saved.checkpoint is not None
            )
            if reusable:
                assert saved is not None
                assert saved.checkpoint is not None
                checkpoint_data = _checkpoint_data(saved.checkpoint)
                if checkpoint_data is not None:
                    last_checkpoint_data = checkpoint_data
                continue
            prior_attempts = 0 if saved is None else saved.attempts
            runnable.append(
                (
                    stage,
                    StageRecord(
                        name=stage.name,
                        position=stage.position,
                        status=StageStatus.PENDING,
                        fingerprint=expected,
                        attempts=prior_attempts,
                    ),
                )
            )

        if not runnable:
            restored = last_checkpoint_data or _record_result_data(record)
            if restored is not None:
                return restored
            context.warnings.append("Reusable stage checkpoints contain no research result.")
            return _failed_data(record, context)

        def start_stage(stage: Stage, pending: StageRecord) -> StageRecord:
            running = pending.model_copy(
                update={
                    "status": StageStatus.RUNNING,
                    "attempts": pending.attempts + 1,
                    "started_at": datetime.now(UTC),
                    "finished_at": None,
                    "checkpoint": None,
                    "error": None,
                }
            )
            self._store.save_stage(record.run_id, running)
            return running

        first_stage, first_pending = runnable[0]
        first_running = start_stage(first_stage, first_pending)

        try:
            data = await self._scenario.run(context)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            self._store.save_stage(
                record.run_id,
                first_running.model_copy(
                    update={
                        "status": StageStatus.FAILED,
                        "error": message,
                        "finished_at": datetime.now(UTC),
                    }
                ),
            )
            self._store.finish(record.run_id, status=RunStatus.FAILED, error=message)
            context.errors.append(message)
            return _failed_data(record, context)

        for index, (stage, pending) in enumerate(runnable):
            running = first_running if index == 0 else start_stage(stage, pending)
            self._store.save_stage(
                record.run_id,
                running.model_copy(
                    update={
                        "status": StageStatus.COMPLETED,
                        "checkpoint": _stage_checkpoint(stage, data),
                        "error": None,
                        "finished_at": datetime.now(UTC),
                    }
                ),
            )
        return data
