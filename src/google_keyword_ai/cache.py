import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import structlog
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from google_keyword_ai.config import Settings

PARSER_VERSION: str = "1"
_PURGE_BATCH_SIZE = 500
logger = structlog.get_logger(__name__)


class CacheCounts(BaseModel):
    entries: int
    expired_entries: int
    payload_bytes: int


def build_cache_key(
    provider: str,
    endpoint: str,
    params: Mapping[str, str],
    account_scope: str,
    parser_version: str,
) -> str:
    canonical = json.dumps(
        {
            "provider": provider,
            "endpoint": endpoint,
            "params": dict(params),
            "account_scope": account_scope,
            "parser_version": parser_version,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class SqliteCache:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings
        self._sweep_attempted = False
        self._bytes_since_eviction = 0

    def get(self, key: str) -> bytes | None:
        if not self._settings.cache_enabled:
            return None

        with self._engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT payload, expires_at FROM cache_entries WHERE key = ?", (key,)
            ).one_or_none()
        if row is None:
            return None

        payload, expires_at = row
        now = datetime.now(UTC)
        if expires_at is not None and self._has_expired(expires_at, now):
            with self._engine.begin() as connection:
                connection.exec_driver_sql(
                    "DELETE FROM cache_entries WHERE key = ? AND expires_at IS ?",
                    (key, expires_at),
                )
            return None
        try:
            with self._engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE cache_entries SET last_read_at = ? WHERE key = ?",
                    (now.isoformat(), key),
                )
        except SQLAlchemyError as exc:
            logger.warning("cache_touch_failed", error=str(exc))
        return bytes(payload)

    @staticmethod
    def _has_expired(expires_at: str, now: datetime) -> bool:
        """Say whether a stored expiry has passed, counting an unreadable one as past.

        `fromisoformat` raises a bare `ValueError` on a damaged timestamp, and
        that is no `GkaiError`: a single corrupt row would reach the caller as a
        crash rather than as a miss. An entry whose expiry cannot be read has no
        knowable lifetime left, so it is dropped and fetched again.

        Parsing is not the only hazard. Everything written here carries a zone,
        because `set` derives the value from `datetime.now(UTC)` -- but a value
        that parses and happens to be naive (`2026-01-01T00:00:00`) reaches the
        comparison and raises `TypeError` there instead, which a guard against
        `ValueError` alone does not catch.
        """
        try:
            deadline = datetime.fromisoformat(expires_at)
        except ValueError:
            return True
        if deadline.tzinfo is None:
            return True
        return deadline <= now

    def purge_expired(self) -> int:
        now = datetime.now(UTC)
        with self._engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT key, expires_at FROM cache_entries WHERE expires_at IS NOT NULL"
            ).all()
        expired = [
            (str(key), str(expires_at))
            for key, expires_at in rows
            if self._has_expired(str(expires_at), now)
        ]

        removed = 0
        for offset in range(0, len(expired), _PURGE_BATCH_SIZE):
            batch = expired[offset : offset + _PURGE_BATCH_SIZE]
            with self._engine.begin() as connection:
                result = connection.exec_driver_sql(
                    "DELETE FROM cache_entries WHERE key = ? AND expires_at IS ?",
                    batch,
                )
            removed += result.rowcount
        return removed

    def purge_all(self) -> int:
        with self._engine.begin() as connection:
            result = connection.exec_driver_sql("DELETE FROM cache_entries")
        return result.rowcount

    def evict_over_limit(self) -> int:
        max_bytes = self._settings.cache_max_bytes
        if max_bytes == 0:
            return 0

        with self._engine.connect() as connection:
            rows = connection.exec_driver_sql(
                """
                SELECT key, last_read_at, length(payload)
                FROM cache_entries
                ORDER BY last_read_at ASC
                """
            ).all()

        remaining_bytes = sum(int(payload_bytes) for _, _, payload_bytes in rows)
        eviction_candidates: list[tuple[str, str | None]] = []
        for key, last_read_at, payload_bytes in rows:
            if remaining_bytes <= max_bytes:
                break
            eviction_candidates.append(
                (str(key), None if last_read_at is None else str(last_read_at))
            )
            remaining_bytes -= int(payload_bytes)

        removed = 0
        for offset in range(0, len(eviction_candidates), _PURGE_BATCH_SIZE):
            batch = eviction_candidates[offset : offset + _PURGE_BATCH_SIZE]
            with self._engine.begin() as connection:
                result = connection.exec_driver_sql(
                    "DELETE FROM cache_entries WHERE key = ? AND last_read_at IS ?",
                    batch,
                )
            removed += result.rowcount
        return removed

    def counts(self) -> CacheCounts:
        now = datetime.now(UTC)
        with self._engine.connect() as connection:
            entries, payload_bytes = connection.exec_driver_sql(
                "SELECT count(*), coalesce(sum(length(payload)), 0) FROM cache_entries"
            ).one()
            expiries = connection.exec_driver_sql(
                "SELECT expires_at FROM cache_entries WHERE expires_at IS NOT NULL"
            ).scalars()
            expired_entries = sum(
                self._has_expired(str(expires_at), now) for expires_at in expiries
            )
        return CacheCounts(
            entries=int(entries),
            expired_entries=expired_entries,
            payload_bytes=int(payload_bytes),
        )

    def set(
        self,
        key: str,
        *,
        provider: str,
        endpoint: str,
        account_scope: str,
        parser_version: str,
        payload: bytes,
        ttl_seconds: int | None,
    ) -> None:
        if not self._settings.cache_enabled:
            return

        if self._settings.cache_sweep_enabled and not self._sweep_attempted:
            self._sweep_attempted = True
            try:
                self.purge_expired()
            except SQLAlchemyError as exc:
                logger.warning("cache_sweep_failed", error=str(exc))
            try:
                self.evict_over_limit()
            except SQLAlchemyError as exc:
                logger.warning("cache_eviction_failed", error=str(exc))
            self._bytes_since_eviction = 0

        created_at = datetime.now(UTC)
        created_at_text = created_at.isoformat()
        expires_at = None if ttl_seconds is None else created_at + timedelta(seconds=ttl_seconds)
        with self._engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO cache_entries (
                    key, provider, endpoint, account_scope, parser_version,
                    payload, created_at, expires_at, last_read_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    provider = excluded.provider,
                    endpoint = excluded.endpoint,
                    account_scope = excluded.account_scope,
                    parser_version = excluded.parser_version,
                    payload = excluded.payload,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at,
                    last_read_at = excluded.last_read_at
                """,
                (
                    key,
                    provider,
                    endpoint,
                    account_scope,
                    parser_version,
                    payload,
                    created_at_text,
                    None if expires_at is None else expires_at.isoformat(),
                    created_at_text,
                ),
            )

        self._bytes_since_eviction += len(payload)
        slack = max(1, self._settings.cache_max_bytes // 100)
        if self._settings.cache_sweep_enabled and self._bytes_since_eviction >= slack:
            try:
                self.evict_over_limit()
            except SQLAlchemyError as exc:
                logger.warning("cache_eviction_failed", error=str(exc))
            self._bytes_since_eviction = 0
