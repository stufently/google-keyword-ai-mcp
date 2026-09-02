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


def _missing[T](run_id: str) -> Envelope[T | None]:
    """Answer that the run does not exist, which is an answer and not a crash.

    The return types say `| None` because that is what this produces. A type
    that cannot express `data: null` compiles happily and then breaks the day
    the function is put behind an interface that validates its output against
    it -- which is exactly how the analysis tools lost their empty answer.
    """
    return cast(
        Envelope[T | None],
        Envelope(
            data=None,
            completeness=Completeness.EMPTY,
            completeness_reason=f"Run {run_id} was not found.",
        ),
    )


def _stored_diagnostics(
    record: RunRecord,
    warnings: list[str],
    errors: list[str],
) -> tuple[list[str], list[str]]:
    """Merge the warnings and errors of a stored envelope into fresh ones.

    Anything the resume itself produced (a stale-version notice, for example)
    is kept and comes first; the stored entries are appended without
    duplicates, so a run resumed twice does not grow its own diagnostics.
    """
    stored = record.result or {}
    for key, target in (("warnings", warnings), ("errors", errors)):
        values = stored.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str) and value not in target:
                target.append(value)
    return warnings, errors


def run_show(settings: Settings, run_id: str) -> Envelope[RunRecord | None]:
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


def run_export(settings: Settings, run_id: str) -> Envelope[dict[str, object] | None]:
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


async def _resume_async(settings: Settings, record: RunRecord) -> Envelope[ResearchData | None]:
    engine = open_database(settings)
    try:
        store = RunStore(engine)
        current = store.get(record.run_id)
        if current is None:
            return _missing(record.run_id)
        market = Market.parse(current.language, current.country)
        cache = SqliteCache(engine, settings)
        async with _live_context(settings, market, current.budget, cache) as context:
            scenario = _scenario_for_name(current.scenario, current.target, current.seed_keyword)
            stages = scenario_stages(
                current.scenario,
                target=current.target,
                market=market,
                budget=current.budget,
                seed_keyword=current.seed_keyword,
            )
            executor = RunExecutor(store, scenario, stages)
            data = await executor.execute(current, context, resume=True)
            if current.limit is not None:
                data.keywords = data.keywords[: current.limit]
            warnings = list(context.warnings)
            errors = list(context.errors)
            if not executor.replayed:
                # Nothing was collected: every checkpoint was still valid. The
                # fresh context therefore knows of no warnings, and rebuilding
                # the envelope from it would silently promote a stored
                # `partial` to `complete` and then overwrite the saved result
                # with that lie. Carry the original diagnostics forward.
                warnings, errors = _stored_diagnostics(current, warnings, errors)
                if current.result is None:
                    # The run was interrupted between its last checkpoint and
                    # the envelope, so its warnings were never written down.
                    # The keywords survive in the checkpoints, but whatever
                    # went wrong while collecting them is gone — say so rather
                    # than present the leftovers as a complete result.
                    warnings.append(
                        "The interrupted run was restored from stage checkpoints; "
                        "any warnings from the original attempt are unavailable."
                    )
            envelope = _envelope_for_research(
                data,
                warnings,
                errors,
                run_id=current.run_id,
            )
            refreshed = store.get(current.run_id)
            failed = refreshed is not None and refreshed.status is RunStatus.FAILED
            store.finish(
                current.run_id,
                status=RunStatus.FAILED if failed else RunStatus.COMPLETED,
                result=envelope.to_wire(),
                error=errors[0] if failed and errors else None,
            )
            if not failed:
                store.set_versions(
                    current.run_id,
                    app_version=__version__,
                    parser_version=PARSER_VERSION,
                )
            return cast("Envelope[ResearchData | None]", envelope)
    finally:
        engine.dispose()


def run_resume(settings: Settings, run_id: str) -> Envelope[ResearchData | None]:
    engine = open_database(settings)
    try:
        record = RunStore(engine).get(run_id)
    finally:
        engine.dispose()
    if record is None:
        return _missing(run_id)
    return anyio.run(partial(_resume_async, settings, record))


def run_rerun(settings: Settings, run_id: str) -> Envelope[ResearchData | None]:
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
        seed_keyword=record.seed_keyword,
        limit=record.limit,
        save_run=True,
    )
    return cast("Envelope[ResearchData | None]", result)
