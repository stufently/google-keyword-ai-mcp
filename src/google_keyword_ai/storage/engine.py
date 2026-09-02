from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine

from google_keyword_ai.config import Settings
from google_keyword_ai.storage.migrations import apply_migrations


def database_path(settings: Settings) -> Path:
    return settings.data_dir / "gkai.sqlite3"


BUSY_TIMEOUT_MS = 5000

PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
)


def apply_sqlite_pragmas(dbapi_connection: Any) -> None:
    """Apply the connection settings this project depends on.

    Kept as a named function rather than an inline closure so tests can call it
    against a connection whose values differ from the driver defaults. Python's
    sqlite3 already opens with a five second busy timeout, so asserting the
    value on a fresh connection proves nothing about our own code.
    """
    previous_autocommit = dbapi_connection.autocommit
    dbapi_connection.autocommit = True
    cursor = dbapi_connection.cursor()
    try:
        for statement in PRAGMAS:
            cursor.execute(statement)
    finally:
        cursor.close()
        dbapi_connection.autocommit = previous_autocommit


def create_engine_for(settings: Settings) -> Engine:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    url = URL.create("sqlite", database=str(database_path(settings)))
    engine = create_engine(url, connect_args={"autocommit": False})

    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        apply_sqlite_pragmas(dbapi_connection)

    return engine


def open_database(settings: Settings) -> Engine:
    engine = create_engine_for(settings)
    apply_migrations(engine)
    return engine
