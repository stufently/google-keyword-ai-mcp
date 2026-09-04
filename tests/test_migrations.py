from multiprocessing import get_context
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Barrier
from pathlib import Path

import pytest
from sqlalchemy import inspect

from google_keyword_ai.config import Settings
from google_keyword_ai.errors import InvalidConfigurationError
from google_keyword_ai.storage.engine import create_engine_for, open_database
from google_keyword_ai.storage.migrations import MIGRATIONS, SCHEMA_VERSION, apply_migrations

# Enough rounds that a fault showing up in roughly one attempt in eight is
# caught essentially every time, and few enough to stay under a second or two.
# A probabilistic guard, deliberately: the journal-mode half of this race
# depends on how the processes interleave, and no arrangement of them caught it
# every time — four contenders over eight rounds detected a reintroduced fault
# about half the time, eight contenders over three rounds rather less. The
# evidence that the fix works is a measurement rather than this test: with the
# fault in place the race failed 3 times in 20, with it fixed 0 times in 25.
# What this test does catch every time is the migration half — a version read
# outside the transaction, or a migration that does not take the write lock.
_RACE_ROUNDS = 6
_RACE_PROCESSES = 4


def _open_database_after_barrier(data_dir: Path, barrier: Barrier, results: Queue[str]) -> None:
    try:
        barrier.wait()
        engine = open_database(Settings(data_dir=data_dir))
    except Exception as exc:
        results.put(str(exc))
    else:
        engine.dispose()
        results.put("ok")


def _upgrade_to(engine: object, version: int) -> None:
    """Build a database at exactly `version` by replaying its own migrations."""
    with engine.begin() as connection:  # type: ignore[attr-defined]
        for migration in MIGRATIONS[:version]:
            migration(connection)
        connection.exec_driver_sql(f"PRAGMA user_version={version}")


def test_fresh_database_has_every_table_at_the_current_version(tmp_path: Path) -> None:
    engine = open_database(Settings(data_dir=tmp_path / "fresh"))
    try:
        with engine.connect() as connection:
            version = connection.exec_driver_sql("PRAGMA user_version").scalar_one()
        assert version == SCHEMA_VERSION
        assert {"cache_entries", "runs", "run_stages"} <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_v1_database_upgrades_without_losing_cache_rows(tmp_path: Path) -> None:
    engine = create_engine_for(Settings(data_dir=tmp_path / "upgrade"))
    try:
        _upgrade_to(engine, 1)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO cache_entries (
                    key, provider, endpoint, account_scope, parser_version,
                    payload, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("kept", "fake", "/v1", "", "1", b"payload", "2026-09-02T00:00:00+00:00", None),
            )

        assert apply_migrations(engine) == SCHEMA_VERSION
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT payload FROM cache_entries WHERE key = 'kept'"
                ).scalar_one()
                == b"payload"
            )
            assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
    finally:
        engine.dispose()


def test_v2_run_rows_survive_the_upgrade_and_read_back_without_a_request(tmp_path: Path) -> None:
    """A run saved before v3 has no seed keyword and no limit, and says so.

    The columns were added because `resume` and `rerun` rebuilt the request
    from the record; a row written by an older version genuinely does not know
    them, and NULL is the honest answer rather than a guess.
    """
    engine = create_engine_for(Settings(data_dir=tmp_path / "v2"))
    try:
        _upgrade_to(engine, 2)
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
                    "run_old",
                    "competitor",
                    "example.com",
                    "en",
                    "US",
                    "completed",
                    "0.0.1",
                    "1",
                    "{}",
                    "{}",
                    None,
                    None,
                    "2026-09-02T00:00:00+00:00",
                    "2026-09-02T00:00:00+00:00",
                ),
            )

        assert apply_migrations(engine) == SCHEMA_VERSION
        with engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT target, seed_keyword, result_limit FROM runs WHERE run_id = 'run_old'"
            ).one()
        assert row == ("example.com", None, None)
    finally:
        engine.dispose()


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    engine = create_engine_for(Settings(data_dir=tmp_path / "idempotent"))
    try:
        assert apply_migrations(engine) == SCHEMA_VERSION
        assert apply_migrations(engine) == SCHEMA_VERSION
        assert inspect(engine).get_table_names().count("runs") == 1
    finally:
        engine.dispose()


def test_a_database_from_the_future_is_rejected(tmp_path: Path) -> None:
    engine = create_engine_for(Settings(data_dir=tmp_path / "future"))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        with pytest.raises(InvalidConfigurationError, match="newer"):
            apply_migrations(engine)
    finally:
        engine.dispose()


def test_four_processes_open_one_fresh_database_at_once(tmp_path: Path) -> None:
    """Opening one fresh database from four processes must succeed in all four.

    This is the ordinary case, not an exotic one: `open_database` runs on every
    `gkai` invocation and on MCP server startup, so a CLI call landing while the
    server boots is two processes creating one file.

    The race is run several times over, each round on its own fresh database.
    That is not a retry — no round is allowed to fail — it is what makes the
    test a detector at all: a race that shows up in one attempt out of eight is
    invisible to a test that attempts it once, and the two defects behind this
    milestone were both of that shape.
    """
    context = get_context("spawn")
    for round_number in range(_RACE_ROUNDS):
        barrier = context.Barrier(_RACE_PROCESSES)
        results: Queue[str] = context.Queue()
        processes = [
            context.Process(
                target=_open_database_after_barrier,
                args=(tmp_path / f"concurrent-{round_number}", barrier, results),
            )
            for _process_number in range(_RACE_PROCESSES)
        ]

        for process in processes:
            process.start()

        try:
            for process in processes:
                process.join(timeout=30)
            assert all(not process.is_alive() for process in processes)
            assert [results.get(timeout=5) for _ in processes] == ["ok"] * _RACE_PROCESSES
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join()
            results.close()
            results.join_thread()
