from collections.abc import Callable

from sqlalchemy.engine import Connection, Engine

from google_keyword_ai.errors import InvalidConfigurationError

SCHEMA_VERSION = 2


def _migration_1(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE cache_entries (
            key TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            account_scope TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            payload BLOB NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE INDEX ix_cache_entries_expires_at ON cache_entries (expires_at)"
    )


def _migration_2(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            scenario TEXT NOT NULL,
            target TEXT NOT NULL,
            language TEXT NOT NULL,
            country TEXT NOT NULL,
            status TEXT NOT NULL,
            app_version TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            budget TEXT NOT NULL,
            config_snapshot TEXT NOT NULL,
            result TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.exec_driver_sql("CREATE INDEX ix_runs_created_at ON runs (created_at)")
    connection.exec_driver_sql(
        """
        CREATE TABLE run_stages (
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            position INTEGER NOT NULL,
            status TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            checkpoint TEXT,
            error TEXT,
            started_at TEXT,
            finished_at TEXT,
            PRIMARY KEY (run_id, name),
            CHECK (status != 'completed' OR checkpoint IS NOT NULL)
        )
        """
    )


MIGRATIONS: list[Callable[[Connection], None]] = [_migration_1, _migration_2]


def apply_migrations(engine: Engine) -> int:
    with engine.connect() as connection:
        current_version = int(connection.exec_driver_sql("PRAGMA user_version").scalar_one())

    if current_version > SCHEMA_VERSION:
        raise InvalidConfigurationError(
            "Database schema is newer than this version of google-keyword-ai: "
            f"database={current_version}, supported={SCHEMA_VERSION}."
        )

    for migration_index in range(current_version, SCHEMA_VERSION):
        with engine.begin() as connection:
            MIGRATIONS[migration_index](connection)
            connection.exec_driver_sql(f"PRAGMA user_version={migration_index + 1}")

    return SCHEMA_VERSION
