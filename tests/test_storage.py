import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine

from google_keyword_ai.config import Settings
from google_keyword_ai.errors import InvalidConfigurationError
from google_keyword_ai.storage import migrations
from google_keyword_ai.storage.engine import (
    BUSY_TIMEOUT_MS,
    apply_sqlite_pragmas,
    create_engine_for,
    database_path,
    open_database,
)
from google_keyword_ai.storage.migrations import SCHEMA_VERSION, apply_migrations


def test_database_path_is_inside_data_dir(settings: Settings) -> None:
    assert database_path(settings) == settings.data_dir / "gkai.sqlite3"


def test_pragma_and_cache_schema_are_created(settings: Settings) -> None:
    engine = open_database(settings)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION

        inspector = inspect(engine)
        assert "cache_entries" in inspector.get_table_names()
        columns = {column["name"] for column in inspector.get_columns("cache_entries")}
        assert columns == {
            "key",
            "provider",
            "endpoint",
            "account_scope",
            "parser_version",
            "payload",
            "created_at",
            "expires_at",
            "last_read_at",
        }
        assert any(
            index["column_names"] == ["expires_at"]
            for index in inspector.get_indexes("cache_entries")
        )
    finally:
        engine.dispose()


def test_migrations_are_idempotent(settings: Settings) -> None:
    engine = create_engine_for(settings)
    try:
        assert apply_migrations(engine) == SCHEMA_VERSION
        assert apply_migrations(engine) == SCHEMA_VERSION
        assert inspect(engine).get_table_names().count("cache_entries") == 1
    finally:
        engine.dispose()


def test_future_database_is_rejected(settings: Settings) -> None:
    engine = create_engine_for(settings)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION + 1}")

        with pytest.raises(InvalidConfigurationError, match="newer"):
            apply_migrations(engine)
    finally:
        engine.dispose()


def test_create_engine_creates_data_directory(settings: Settings) -> None:
    assert not settings.data_dir.exists()

    engine: Engine = create_engine_for(settings)
    try:
        assert settings.data_dir.is_dir()
    finally:
        engine.dispose()


def test_migration_failure_rolls_back_ddl(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_migration(connection: Connection) -> None:
        connection.exec_driver_sql("CREATE TABLE should_rollback (id INTEGER)")
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(migrations, "MIGRATIONS", [failing_migration])
    engine = create_engine_for(settings)
    try:
        with pytest.raises(RuntimeError, match="injected migration failure"):
            apply_migrations(engine)

        assert "should_rollback" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == 0
    finally:
        engine.dispose()


def test_sqlite_url_preserves_question_mark_in_data_path(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "has?mark")

    engine = create_engine_for(settings)
    try:
        assert engine.url.database == str(database_path(settings))
    finally:
        engine.dispose()


def test_pragmas_are_applied_over_non_default_values(settings: Settings) -> None:
    """Guard against a tautological assertion.

    Python's sqlite3 opens connections with a five second busy timeout already,
    so reading 5000 back from a fresh connection says nothing about whether we
    set it. Start from values that differ from every default and check that our
    hook overwrites each one.
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(database_path(settings), autocommit=False)
    try:
        raw.autocommit = True
        raw.execute("PRAGMA busy_timeout=113")
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("PRAGMA synchronous=FULL")
        raw.autocommit = False
        assert raw.execute("PRAGMA busy_timeout").fetchone()[0] == 113

        apply_sqlite_pragmas(raw)

        assert raw.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert raw.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        raw.close()
