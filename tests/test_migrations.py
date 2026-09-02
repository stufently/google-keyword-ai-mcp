from pathlib import Path

import pytest
from sqlalchemy import inspect

from google_keyword_ai.config import Settings
from google_keyword_ai.errors import InvalidConfigurationError
from google_keyword_ai.storage.engine import create_engine_for, open_database
from google_keyword_ai.storage.migrations import MIGRATIONS, SCHEMA_VERSION, apply_migrations


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
