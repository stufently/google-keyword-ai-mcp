import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy.engine import Connection, Engine

from google_keyword_ai.errors import InvalidConfigurationError
from google_keyword_ai.pipeline.budget import Budget


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class StageRecord(BaseModel):
    name: str
    position: int
    status: StageStatus
    fingerprint: str
    attempts: int = 0
    checkpoint: dict[str, object] | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RunRecord(BaseModel):
    run_id: str
    scenario: str
    target: str
    language: str
    country: str
    status: RunStatus
    # The rest of the original request. `resume` and `rerun` rebuild the
    # scenario from this record, so anything missing here is silently dropped
    # and the repeat answers a different question than the user asked.
    seed_keyword: str | None = None
    limit: int | None = None
    app_version: str
    parser_version: str
    budget: Budget
    config_snapshot: dict[str, object]
    result: dict[str, object] | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    stages: list[StageRecord]


def new_run_id() -> str:
    return f"run_{uuid4().hex[:26]}"


def stage_fingerprint(name: str, payload: Mapping[str, object]) -> str:
    canonical = json.dumps(
        {"name": name, **payload},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None) -> dict[str, object] | None:
    if value is None:
        return None
    loaded: object = json.loads(value)
    if not isinstance(loaded, dict):
        raise InvalidConfigurationError("Stored run JSON must be an object.")
    return loaded


class RunStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(self, record: RunRecord) -> None:
        with self._engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO runs (
                    run_id, scenario, target, language, country, status,
                    seed_keyword, result_limit,
                    app_version, parser_version, budget, config_snapshot,
                    result, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.scenario,
                    record.target,
                    record.language,
                    record.country,
                    record.status.value,
                    record.seed_keyword,
                    record.limit,
                    record.app_version,
                    record.parser_version,
                    _json(record.budget.model_dump(mode="json")),
                    _json(record.config_snapshot),
                    None if record.result is None else _json(record.result),
                    record.error,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
            for stage in record.stages:
                self._save_stage(connection, record.run_id, stage)

    def get(self, run_id: str) -> RunRecord | None:
        with self._engine.connect() as connection:
            row = connection.exec_driver_sql(
                """
                SELECT run_id, scenario, target, language, country, status,
                       app_version, parser_version, budget, config_snapshot,
                       result, error, created_at, updated_at,
                       seed_keyword, result_limit
                FROM runs WHERE run_id = ?
                """,
                (run_id,),
            ).one_or_none()
            if row is None:
                return None
            stage_rows = connection.exec_driver_sql(
                """
                SELECT name, position, status, fingerprint, attempts,
                       checkpoint, error, started_at, finished_at
                FROM run_stages WHERE run_id = ? ORDER BY position, name
                """,
                (run_id,),
            ).all()
        return self._record_from_rows(row, stage_rows)

    def list(self, limit: int = 20) -> list[RunRecord]:
        if limit <= 0:
            raise InvalidConfigurationError("Run list limit must be positive.")
        with self._engine.connect() as connection:
            run_ids = connection.exec_driver_sql(
                "SELECT run_id FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
                (limit,),
            ).scalars()
            ids = list(run_ids)
        return [record for run_id in ids if (record := self.get(run_id)) is not None]

    def save_stage(self, run_id: str, stage: StageRecord) -> None:
        with self._engine.begin() as connection:
            self._save_stage(connection, run_id, stage)

    def replace_stages(
        self,
        run_id: str,
        *,
        scenario: str,
        stages: Sequence[StageRecord],
    ) -> None:
        with self._engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE runs SET scenario = ?, updated_at = ? WHERE run_id = ?",
                (scenario, datetime.now(UTC).isoformat(), run_id),
            )
            connection.exec_driver_sql("DELETE FROM run_stages WHERE run_id = ?", (run_id,))
            for stage in stages:
                self._save_stage(connection, run_id, stage)

    @staticmethod
    def _save_stage(connection: Connection, run_id: str, stage: StageRecord) -> None:
        if stage.status is StageStatus.COMPLETED and stage.checkpoint is None:
            raise InvalidConfigurationError("A completed run stage must have an atomic checkpoint.")
        connection.exec_driver_sql(
            """
            INSERT INTO run_stages (
                run_id, name, position, status, fingerprint, attempts,
                checkpoint, error, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, name) DO UPDATE SET
                position = excluded.position,
                status = excluded.status,
                fingerprint = excluded.fingerprint,
                attempts = excluded.attempts,
                checkpoint = excluded.checkpoint,
                error = excluded.error,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at
            """,
            (
                run_id,
                stage.name,
                stage.position,
                stage.status.value,
                stage.fingerprint,
                stage.attempts,
                None if stage.checkpoint is None else _json(stage.checkpoint),
                stage.error,
                None if stage.started_at is None else stage.started_at.isoformat(),
                None if stage.finished_at is None else stage.finished_at.isoformat(),
            ),
        )

    def finish(
        self,
        run_id: str,
        *,
        status: RunStatus,
        result: dict[str, object] | None = None,
        error: str | None = None,
    ) -> None:
        with self._engine.begin() as connection:
            connection.exec_driver_sql(
                """
                UPDATE runs
                SET status = ?, result = ?, error = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    status.value,
                    None if result is None else _json(result),
                    error,
                    datetime.now(UTC).isoformat(),
                    run_id,
                ),
            )

    def set_versions(self, run_id: str, *, app_version: str, parser_version: str) -> None:
        with self._engine.begin() as connection:
            connection.exec_driver_sql(
                """
                UPDATE runs SET app_version = ?, parser_version = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (app_version, parser_version, datetime.now(UTC).isoformat(), run_id),
            )

    def delete(self, run_id: str) -> bool:
        with self._engine.begin() as connection:
            result = connection.exec_driver_sql("DELETE FROM runs WHERE run_id = ?", (run_id,))
        return bool(result.rowcount)

    @staticmethod
    def _record_from_rows(row: Any, stage_rows: Any) -> RunRecord:
        budget = _loads(row[8])
        config_snapshot = _loads(row[9])
        assert budget is not None
        assert config_snapshot is not None
        stages = [
            StageRecord(
                name=stage[0],
                position=stage[1],
                status=stage[2],
                fingerprint=stage[3],
                attempts=stage[4],
                checkpoint=_loads(stage[5]),
                error=stage[6],
                started_at=stage[7],
                finished_at=stage[8],
            )
            for stage in stage_rows
        ]
        return RunRecord(
            run_id=row[0],
            scenario=row[1],
            target=row[2],
            language=row[3],
            country=row[4],
            status=row[5],
            app_version=row[6],
            parser_version=row[7],
            budget=Budget.model_validate(budget),
            config_snapshot=config_snapshot,
            result=_loads(row[10]),
            error=row[11],
            created_at=row[12],
            updated_at=row[13],
            seed_keyword=row[14],
            limit=row[15],
            stages=stages,
        )
