import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy.engine import Engine

from google_keyword_ai.config import Settings

PARSER_VERSION: str = "1"


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
        if expires_at is not None and self._has_expired(expires_at):
            with self._engine.begin() as connection:
                connection.exec_driver_sql("DELETE FROM cache_entries WHERE key = ?", (key,))
            return None
        return bytes(payload)

    @staticmethod
    def _has_expired(expires_at: str) -> bool:
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
        return deadline <= datetime.now(UTC)

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

        created_at = datetime.now(UTC)
        expires_at = None if ttl_seconds is None else created_at + timedelta(seconds=ttl_seconds)
        with self._engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO cache_entries (
                    key, provider, endpoint, account_scope, parser_version,
                    payload, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    provider = excluded.provider,
                    endpoint = excluded.endpoint,
                    account_scope = excluded.account_scope,
                    parser_version = excluded.parser_version,
                    payload = excluded.payload,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (
                    key,
                    provider,
                    endpoint,
                    account_scope,
                    parser_version,
                    payload,
                    created_at.isoformat(),
                    None if expires_at is None else expires_at.isoformat(),
                ),
            )
