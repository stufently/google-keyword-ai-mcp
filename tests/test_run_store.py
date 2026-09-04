import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from google_keyword_ai import __version__
from google_keyword_ai.cache import PARSER_VERSION
from google_keyword_ai.config import Settings, masked_dump
from google_keyword_ai.errors import InvalidConfigurationError
from google_keyword_ai.pipeline.budget import Budget
from google_keyword_ai.pipeline.runs import (
    RunRecord,
    RunStatus,
    RunStore,
    StageRecord,
    StageStatus,
    new_run_id,
)
from google_keyword_ai.storage.engine import open_database


def _record(
    *,
    created_at: datetime | None = None,
    settings: Settings | None = None,
) -> RunRecord:
    now = datetime.now(UTC) if created_at is None else created_at
    active_settings = Settings() if settings is None else settings
    return RunRecord(
        run_id=new_run_id(),
        scenario="niche",
        target="topic",
        language="en",
        country="US",
        status=RunStatus.RUNNING,
        app_version=__version__,
        parser_version=PARSER_VERSION,
        budget=Budget(),
        config_snapshot=masked_dump(active_settings),
        created_at=now,
        updated_at=now,
        stages=[
            StageRecord(
                name="expand",
                position=0,
                status=StageStatus.PENDING,
                fingerprint="a" * 32,
            )
        ],
    )


def test_run_id_format() -> None:
    assert re.fullmatch(r"run_[0-9a-f]{26}", new_run_id())


def test_create_get_list_upsert_and_delete(tmp_path: Path) -> None:
    engine = open_database(Settings(data_dir=tmp_path / "store"))
    try:
        store = RunStore(engine)
        older = _record(created_at=datetime.now(UTC) - timedelta(minutes=1))
        newer = _record(created_at=datetime.now(UTC))
        store.create(older)
        store.create(newer)

        assert store.get(older.run_id) == older
        records, unreadable = store.list()
        assert [record.run_id for record in records] == [newer.run_id, older.run_id]
        assert unreadable == []

        completed = older.stages[0].model_copy(
            update={
                "status": StageStatus.COMPLETED,
                "attempts": 1,
                "checkpoint": {"keywords": ["one"]},
                "finished_at": datetime.now(UTC),
            }
        )
        store.save_stage(older.run_id, completed)
        saved = store.get(older.run_id)
        assert saved is not None
        assert saved.stages[0] == completed
        assert store.delete(older.run_id) is True
        assert store.delete(older.run_id) is False
        assert store.get(older.run_id) is None
    finally:
        engine.dispose()


def test_secret_values_are_absent_from_config_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "never-store-this-token"
    monkeypatch.setenv("GKAI_GOOGLE_ADS_DEVELOPER_TOKEN", secret)
    settings = Settings(data_dir=tmp_path / "secret")
    engine = open_database(settings)
    try:
        record = _record(settings=settings)
        RunStore(engine).create(record)
        with engine.connect() as connection:
            stored = connection.exec_driver_sql(
                "SELECT config_snapshot FROM runs WHERE run_id = ?", (record.run_id,)
            ).scalar_one()
        assert secret not in stored
        assert json.loads(stored)["google_ads_developer_token"] == "***"
    finally:
        engine.dispose()


def test_atomic_completed_stage_requires_checkpoint(tmp_path: Path) -> None:
    engine = open_database(Settings(data_dir=tmp_path / "atomic"))
    try:
        store = RunStore(engine)
        record = _record()
        store.create(record)
        invalid = record.stages[0].model_copy(
            update={"status": StageStatus.COMPLETED, "attempts": 1}
        )
        with pytest.raises(InvalidConfigurationError, match="checkpoint"):
            store.save_stage(record.run_id, invalid)
        saved = store.get(record.run_id)
        assert saved is not None
        assert saved.stages[0].status is StageStatus.PENDING
        assert saved.stages[0].checkpoint is None
    finally:
        engine.dispose()


def test_a_damaged_run_row_is_reported_instead_of_crashing(tmp_path: Path) -> None:
    """A row that cannot be parsed is a refusal, not a traceback.

    Four of a run's columns hold JSON and two hold enumerations, all read back
    without conversion. A damaged one raises `JSONDecodeError` or
    `ValidationError` -- both `ValueError`, neither a `GkaiError` -- so `run
    show` on a damaged run printed a traceback where every other unreadable
    stored value in this project produces an envelope.
    """
    settings = Settings(data_dir=tmp_path / "damaged")
    engine = open_database(settings)
    try:
        store = RunStore(engine)
        record = _record(settings=settings)
        store.create(record)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE runs SET budget = ? WHERE run_id = ?", ("{not json", record.run_id)
            )

        with pytest.raises(InvalidConfigurationError) as raised:
            store.get(record.run_id)
    finally:
        engine.dispose()

    assert record.run_id in raised.value.message


def test_one_damaged_row_does_not_hide_the_rest_of_the_history(tmp_path: Path) -> None:
    """The listing is where a caller finds the run to delete.

    Letting one damaged row raise would take the whole history down with it, and
    with it the id of the run that has to go. The readable runs come back, and
    the unreadable one is named beside them.
    """
    settings = Settings(data_dir=tmp_path / "mixed")
    engine = open_database(settings)
    try:
        store = RunStore(engine)
        healthy = _record(settings=settings, created_at=datetime.now(UTC))
        damaged = _record(settings=settings, created_at=datetime.now(UTC) - timedelta(hours=1))
        store.create(healthy)
        store.create(damaged)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE runs SET status = ? WHERE run_id = ?", ("nonsense", damaged.run_id)
            )

        records, unreadable = store.list()
    finally:
        engine.dispose()

    assert [record.run_id for record in records] == [healthy.run_id]
    assert unreadable == [damaged.run_id]
