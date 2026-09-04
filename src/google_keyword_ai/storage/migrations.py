from collections.abc import Callable

from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from google_keyword_ai.errors import InvalidConfigurationError

SCHEMA_VERSION = 4


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


def _migration_3(connection: Connection) -> None:
    # A saved run has to remember the whole request, not just the target. Without
    # the seed keyword a resumed competitor run silently changed shape, and
    # without the limit a resume returned more keywords than the original.
    # Existing rows predate both options and correctly read back as NULL.
    connection.exec_driver_sql("ALTER TABLE runs ADD COLUMN seed_keyword TEXT")
    connection.exec_driver_sql("ALTER TABLE runs ADD COLUMN result_limit INTEGER")


def _migration_4(connection: Connection) -> None:
    connection.exec_driver_sql("ALTER TABLE cache_entries ADD COLUMN last_read_at TEXT")
    connection.exec_driver_sql("UPDATE cache_entries SET last_read_at = created_at")
    connection.exec_driver_sql(
        "CREATE INDEX ix_cache_entries_last_read_at ON cache_entries (last_read_at)"
    )


MIGRATIONS: list[Callable[[Connection], None]] = [
    _migration_1,
    _migration_2,
    _migration_3,
    _migration_4,
]


def _validate_schema_version(connection: Connection) -> int:
    current_version = int(connection.exec_driver_sql("PRAGMA user_version").scalar_one())
    if current_version > SCHEMA_VERSION:
        raise InvalidConfigurationError(
            "Database schema is newer than this version of google-keyword-ai: "
            f"database={current_version}, supported={SCHEMA_VERSION}."
        )
    return current_version


def _begin_immediate(connection: Connection) -> None:
    # DBAPI autocommit=False keeps an empty transaction open between calls.
    connection.exec_driver_sql("COMMIT")
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def apply_migrations(engine: Engine) -> int:
    try:
        # Reading the version needs no write lock; the loop below takes one for
        # each migration, which is where the DDL actually happens.
        with engine.connect() as connection:
            current_version = _validate_schema_version(connection)

        for migration_index in range(current_version, SCHEMA_VERSION):
            with engine.connect() as connection:
                _begin_immediate(connection)
                try:
                    current_version = _validate_schema_version(connection)
                    if current_version <= migration_index:
                        MIGRATIONS[migration_index](connection)
                        connection.exec_driver_sql(f"PRAGMA user_version={migration_index + 1}")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
    except SQLAlchemyError as exc:
        # `InvalidConfigurationError` passes through untouched: it is a
        # `GkaiError`, not a `SQLAlchemyError`, so the refusal of a database
        # newer than this build keeps its own wording.
        raise InvalidConfigurationError(f"Could not apply database migrations: {exc}") from exc

    return SCHEMA_VERSION
