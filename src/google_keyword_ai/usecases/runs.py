from functools import partial
from typing import cast

import anyio

from google_keyword_ai import __version__
from google_keyword_ai.cache import PARSER_VERSION, SqliteCache
from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.market import Market
from google_keyword_ai.pipeline.executor import RunExecutor, scenario_stages
from google_keyword_ai.pipeline.models import ResearchData
from google_keyword_ai.pipeline.runs import RunRecord, RunStatus, RunStore
from google_keyword_ai.storage.engine import open_database
from google_keyword_ai.usecases.research import (
    _envelope_for_research,
    _live_context,
    _scenario_for_name,
    run_research,
)


def _missing[T](run_id: str) -> Envelope[T]:
    return cast(
        Envelope[T],
        Envelope(
            data=None,
            completeness=Completeness.EMPTY,
            completeness_reason=f"Run {run_id} was not found.",
        ),
    )


def run_show(settings: Settings, run_id: str) -> Envelope[RunRecord]:
    engine = open_database(settings)
    try:
        record = RunStore(engine).get(run_id)
    finally:
        engine.dispose()
    if record is None:
        return _missing(run_id)
    return Envelope(data=record)


def run_list(settings: Settings, *, limit: int = 20) -> Envelope[list[RunRecord]]:
    engine = open_database(settings)
    try:
        records = RunStore(engine).list(limit=limit)
    finally:
        engine.dispose()
    return Envelope(data=records)


def run_export(settings: Settings, run_id: str) -> Envelope[dict[str, object]]:
    engine = open_database(settings)
    try:
        record = RunStore(engine).get(run_id)
    finally:
        engine.dispose()
    if record is None:
        return _missing(run_id)
    return Envelope(
        data={
            "record": record.model_dump(mode="json", exclude={"stages", "result"}),
            "stages": [stage.model_dump(mode="json") for stage in record.stages],
            "result": record.result,
        }
    )


async def _resume_async(settings: Settings, record: RunRecord) -> Envelope[ResearchData]:
    engine = open_database(settings)
    try:
        store = RunStore(engine)
        current = store.get(record.run_id)
        if current is None:
            return _missing(record.run_id)
        market = Market.parse(current.language, current.country)
        cache = SqliteCache(engine, settings)
        async with _live_context(settings, market, current.budget, cache) as context:
            scenario = _scenario_for_name(current.scenario, current.target, None)
            stages = scenario_stages(
                current.scenario,
                target=current.target,
                market=market,
                budget=current.budget,
            )
            data = await RunExecutor(store, scenario, stages).execute(
                current,
                context,
                resume=True,
            )
            envelope = _envelope_for_research(
                data,
                context.warnings,
                context.errors,
                run_id=current.run_id,
            )
            refreshed = store.get(current.run_id)
            failed = refreshed is not None and refreshed.status is RunStatus.FAILED
            store.finish(
                current.run_id,
                status=RunStatus.FAILED if failed else RunStatus.COMPLETED,
                result=envelope.to_wire(),
                error=context.errors[0] if failed and context.errors else None,
            )
            if not failed:
                store.set_versions(
                    current.run_id,
                    app_version=__version__,
                    parser_version=PARSER_VERSION,
                )
            return envelope
    finally:
        engine.dispose()


def run_resume(settings: Settings, run_id: str) -> Envelope[ResearchData]:
    engine = open_database(settings)
    try:
        record = RunStore(engine).get(run_id)
    finally:
        engine.dispose()
    if record is None:
        return _missing(run_id)
    return anyio.run(partial(_resume_async, settings, record))


def run_rerun(settings: Settings, run_id: str) -> Envelope[ResearchData]:
    engine = open_database(settings)
    try:
        record = RunStore(engine).get(run_id)
    finally:
        engine.dispose()
    if record is None:
        return _missing(run_id)
    result = run_research(
        settings,
        record.target,
        scenario=record.scenario,
        language=record.language,
        country=record.country,
        budget=record.budget,
        save_run=True,
    )
    return cast(Envelope[ResearchData], result)
