import sqlite3
import time
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine

from google_keyword_ai.config import Settings
from google_keyword_ai.storage.migrations import apply_migrations


def database_path(settings: Settings) -> Path:
    return settings.data_dir / "gkai.sqlite3"


BUSY_TIMEOUT_MS = 5000

_WAL_PRAGMA = "PRAGMA journal_mode=WAL"

# The busy timeout comes first on purpose: nothing can wait on a lock until it
# is in force. The journal mode is applied through `_enable_wal` rather than
# executed directly, because that one switch needs handling the others do not.
PRAGMAS: tuple[str, ...] = (
    f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}",
    _WAL_PRAGMA,
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
            if statement == _WAL_PRAGMA:
                _enable_wal(cursor)
            else:
                cursor.execute(statement)
    finally:
        cursor.close()
        dbapi_connection.autocommit = previous_autocommit


def _enable_wal(cursor: Any) -> None:
    """Put the database in WAL mode, tolerating another process doing the same.

    The journal mode is a persistent property of the FILE, not of this
    connection: once any process has set it, every later connection opens a WAL
    database and nobody needs to set it again. Switching it, though, needs
    exclusive access, and SQLite's busy handler does not cover that switch --
    `busy_timeout` buys nothing here, which was measured: with the timeout
    already in force, four processes opening one fresh database at the same
    moment still produced one winner and three `database is locked` failures,
    raised before any migration ran.

    So the switch is retried inside the same time budget the busy timeout gives
    every other statement. Once the neighbour that held the lock is done the
    database is already in WAL, and then the pragma is a no-op that needs no
    exclusive lock -- the retry succeeds rather than having to detect anything.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError:
            # Nothing to check between attempts: on a database that is already
            # in WAL the pragma is a no-op needing no exclusive lock, so the
            # very next attempt succeeds the moment the neighbour is done.
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


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
