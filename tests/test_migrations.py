from pathlib import Path

import pytest
from sqlalchemy import inspect

from google_keyword_ai.config import Settings
from google_keyword_ai.errors import InvalidConfigurationError
from google_keyword_ai.storage.engine import create_engine_for, open_database
from google_keyword_ai.storage.migrations import MIGRATIONS, SCHEMA_VERSION, apply_migrations


def test_fresh_database_has_v2_tables(tmp_path: Path) -> None:
    engine = open_database(Settings(data_dir=tmp_path / "fresh"))
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == 2
        assert {"cache_entries", "runs", "run_stages"} <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_v1_database_upgrades_without_losing_cache_rows(tmp_path: Path) -> None:
    engine = create_engine_for(Settings(data_dir=tmp_path / "upgrade"))
    try:
        with engine.begin() as connection:
            MIGRATIONS[0](connection)
            connection.exec_driver_sql("PRAGMA user_version=1")
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
            assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == 2
    finally:
        engine.dispose()


def test_v2_migrations_are_idempotent(tmp_path: Path) -> None:
    engine = create_engine_for(Settings(data_dir=tmp_path / "idempotent"))
    try:
        assert apply_migrations(engine) == 2
        assert apply_migrations(engine) == 2
        assert inspect(engine).get_table_names().count("runs") == 1
    finally:
        engine.dispose()


def test_v3_database_is_rejected(tmp_path: Path) -> None:
    engine = create_engine_for(Settings(data_dir=tmp_path / "future"))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA user_version=3")
        with pytest.raises(InvalidConfigurationError, match="newer"):
            apply_migrations(engine)
    finally:
        engine.dispose()
