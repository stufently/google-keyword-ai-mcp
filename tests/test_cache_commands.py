import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError
from typer.testing import CliRunner

from google_keyword_ai.cli import main as cli_main
from google_keyword_ai.config import Settings
from google_keyword_ai.storage.engine import database_path, open_database
from google_keyword_ai.usecases import cache as cache_usecase
from google_keyword_ai.usecases.cache import run_cache_purge, run_cache_status


def _insert_cache_rows(settings: Settings, rows: list[tuple[str, bytes, str | None]]) -> None:
    now = datetime.now(UTC).isoformat()
    engine = open_database(settings)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO cache_entries (
                    key, provider, endpoint, account_scope, parser_version,
                    payload, created_at, expires_at
                ) VALUES (?, 'test', 'https://example.test', '', '1', ?, ?, ?)
                """,
                [(key, payload, now, expires_at) for key, payload, expires_at in rows],
            )
    finally:
        engine.dispose()


def _invoke(settings: Settings, monkeypatch: pytest.MonkeyPatch, args: list[str]) -> Any:
    monkeypatch.setattr(cli_main, "load_settings", lambda: settings)
    return CliRunner().invoke(cli_main.app, args)


def _page_count(settings: Settings) -> int:
    engine = open_database(settings)
    try:
        with engine.connect() as connection:
            return int(connection.exec_driver_sql("PRAGMA page_count").scalar_one())
    finally:
        engine.dispose()


def _database_bytes_on_disk(settings: Settings) -> int:
    path = database_path(settings)
    return sum(
        candidate.stat().st_size
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )


def test_cache_status_reports_database_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "status")
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    _insert_cache_rows(
        settings,
        [("expired", b"one", past), ("damaged", b"twenty", "bad"), ("fresh", b"333", future)],
    )

    holding_engine = open_database(settings)
    try:
        with holding_engine.connect() as connection:
            connection.exec_driver_sql("SELECT count(*) FROM cache_entries").scalar_one()
            result = _invoke(settings, monkeypatch, ["cache", "status", "--format", "json"])

            assert result.exit_code == 0, result.output
            sidecar_bytes = sum(
                Path(f"{database_path(settings)}{suffix}").stat().st_size
                for suffix in ("-wal", "-shm")
            )
            assert sidecar_bytes > 0
            envelope = json.loads(result.stdout)
            assert envelope["completeness"] == "complete"
            assert envelope["data"]["entries"] == 3
            assert envelope["data"]["expired_entries"] == 2
            assert envelope["data"]["payload_bytes"] == 12
            assert envelope["data"]["database_bytes"] == _database_bytes_on_disk(settings)
    finally:
        holding_engine.dispose()


@pytest.mark.parametrize(
    ("args", "remaining_cache"),
    [
        pytest.param(["cache", "purge"], 1, id="expired"),
        pytest.param(["cache", "purge", "--all"], 0, id="all"),
    ],
)
def test_cache_purge_keeps_runs_and_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    remaining_cache: int,
) -> None:
    settings = Settings(data_dir=tmp_path / f"purge-{remaining_cache}")
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    _insert_cache_rows(settings, [("expired", b"old", past), ("fresh", b"new", future)])
    now = datetime.now(UTC).isoformat()
    engine = open_database(settings)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO runs (
                    run_id, scenario, target, language, country, status,
                    app_version, parser_version, budget, config_snapshot,
                    result, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "run_saved",
                    "topic",
                    "shoes",
                    "en",
                    "US",
                    "running",
                    "0.1.0",
                    "1",
                    "{}",
                    "{}",
                    None,
                    None,
                    now,
                    now,
                ),
            )
            connection.exec_driver_sql(
                """
                INSERT INTO run_stages (
                    run_id, name, position, status, fingerprint, attempts
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("run_saved", "suggest", 0, "pending", "fingerprint", 0),
            )
    finally:
        engine.dispose()

    result = _invoke(settings, monkeypatch, [*args, "--format", "json"])

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert envelope["data"]["removed"] == 2 - remaining_cache
    engine = open_database(settings)
    try:
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT count(*) FROM cache_entries").scalar_one()
                == remaining_cache
            )
            assert connection.exec_driver_sql("SELECT count(*) FROM runs").scalar_one() == 1
            assert connection.exec_driver_sql("SELECT count(*) FROM run_stages").scalar_one() == 1
    finally:
        engine.dispose()


def test_cache_purge_vacuum_controls_page_reclamation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before_and_after: list[tuple[int, int]] = []
    for name, vacuum in (("without", False), ("with", True)):
        settings = Settings(data_dir=tmp_path / name)
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        _insert_cache_rows(
            settings,
            [(f"key-{index}", b"x" * 4096, future) for index in range(150)],
        )
        before = _page_count(settings)
        assert before > 100
        args = ["cache", "purge", "--all"]
        if vacuum:
            args.append("--vacuum")

        result = _invoke(settings, monkeypatch, args)

        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        assert envelope["data"]["vacuumed"] is vacuum
        before_and_after.append((before, _page_count(settings)))

    assert before_and_after[0][1] == before_and_after[0][0]
    assert before_and_after[1][1] < 20


def test_cache_purge_vacuum_failure_reports_committed_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "vacuum-failure")
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    _insert_cache_rows(settings, [("cached", b"payload", future)])

    def fail_vacuum(_engine: object) -> None:
        raise SQLAlchemyError("vacuum failed")

    monkeypatch.setattr(cache_usecase, "_vacuum", fail_vacuum)

    result = run_cache_purge(settings, purge_all=True, vacuum=True)

    assert result.completeness.value == "partial"
    assert result.data is not None
    assert result.data.removed == 1
    assert result.data.vacuumed is False
    assert result.errors == ["vacuum failed"]
    engine = open_database(settings)
    try:
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT count(*) FROM cache_entries").scalar_one() == 0
            )
    finally:
        engine.dispose()


def test_cache_status_unreadable_database_returns_empty_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(data_dir=tmp_path / "unreadable")
    database_path(settings).mkdir(parents=True)

    usecase_result = run_cache_status(settings)
    result = _invoke(settings, monkeypatch, ["cache", "status", "--format", "json"])

    assert usecase_result.data is None
    assert usecase_result.completeness.value == "empty"
    assert result.exit_code == 1
    envelope = json.loads(result.stdout)
    assert envelope["data"] is None
    assert envelope["completeness"] == "empty"
    assert envelope["completeness_reason"]
    assert envelope["errors"]
