import sqlite3
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from google_keyword_ai.cache import SqliteCache
from google_keyword_ai.config import Settings
from google_keyword_ai.envelope import Completeness, Envelope
from google_keyword_ai.errors import GkaiError
from google_keyword_ai.storage.engine import database_path, open_database


class CacheStatusData(BaseModel):
    """Cache counts and the on-disk size of the whole database, including run history."""

    entries: int
    expired_entries: int
    payload_bytes: int
    database_bytes: int = Field(
        description="Bytes used by the whole database and sidecars, including saved run history."
    )


class CachePurgeData(BaseModel):
    """Purge result whose byte sizes cover the whole database, including run history."""

    scope: str
    removed: int
    vacuumed: bool
    database_bytes_before: int = Field(
        description="Bytes used before purge by the whole database, including saved run history."
    )
    database_bytes_after: int = Field(
        description="Bytes used after purge by the whole database, including saved run history."
    )


def _database_bytes(settings: Settings) -> int:
    path = database_path(settings)
    total = 0
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        with suppress(FileNotFoundError):
            total += candidate.stat().st_size
    return total


def _empty[T](action: str, exc: BaseException) -> Envelope[T | None]:
    error = str(exc)
    reason = f"Could not {action}: {error}"
    return cast(
        "Envelope[T | None]",
        Envelope(
            data=None,
            errors=[error],
            completeness=Completeness.EMPTY,
            completeness_reason=reason,
        ),
    )


def _vacuum(engine: Engine) -> None:
    with engine.connect() as connection:
        raw: Any = connection.connection.driver_connection
        previous_autocommit = raw.autocommit
        raw.autocommit = True
        cursor = raw.cursor()
        try:
            cursor.execute("VACUUM")
        finally:
            cursor.close()
            raw.autocommit = previous_autocommit


def run_cache_status(settings: Settings) -> Envelope[CacheStatusData | None]:
    try:
        engine = open_database(settings)
        try:
            counts = SqliteCache(engine, settings).counts()
        finally:
            engine.dispose()
        size = _database_bytes(settings)
    except (GkaiError, SQLAlchemyError, OSError) as exc:
        return _empty("read cache status", exc)

    return Envelope(
        data=CacheStatusData(
            entries=counts.entries,
            expired_entries=counts.expired_entries,
            payload_bytes=counts.payload_bytes,
            database_bytes=size,
        )
    )


def run_cache_purge(
    settings: Settings, *, purge_all: bool, vacuum: bool
) -> Envelope[CachePurgeData | None]:
    scope = "all" if purge_all else "expired"
    try:
        size_before = _database_bytes(settings)
        engine = open_database(settings)
        try:
            cache = SqliteCache(engine, settings)
            removed = cache.purge_all() if purge_all else cache.purge_expired()
            vacuum_error: BaseException | None = None
            if vacuum:
                try:
                    _vacuum(engine)
                except (SQLAlchemyError, OSError, sqlite3.Error) as exc:
                    vacuum_error = exc
        finally:
            engine.dispose()
        size_after = _database_bytes(settings)
    except (GkaiError, SQLAlchemyError, OSError) as exc:
        return _empty("purge cache", exc)

    data = CachePurgeData(
        scope=scope,
        removed=removed,
        vacuumed=vacuum and vacuum_error is None,
        database_bytes_before=size_before,
        database_bytes_after=size_after,
    )
    if vacuum_error is not None:
        error = str(vacuum_error)
        return Envelope(
            data=data,
            errors=[error],
            completeness=Completeness.PARTIAL,
            completeness_reason=f"Cache entries were removed, but VACUUM failed: {error}",
        )
    return Envelope(data=data)
