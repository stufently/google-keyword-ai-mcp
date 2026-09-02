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
        assert [record.run_id for record in store.list()] == [newer.run_id, older.run_id]

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
