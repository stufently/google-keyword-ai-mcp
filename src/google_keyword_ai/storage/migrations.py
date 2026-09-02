from collections.abc import Callable

from sqlalchemy.engine import Connection, Engine

from google_keyword_ai.errors import InvalidConfigurationError

SCHEMA_VERSION = 1


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


MIGRATIONS: list[Callable[[Connection], None]] = [_migration_1]


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
