from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError

from google_keyword_ai.cache import SqliteCache, build_cache_key
from google_keyword_ai.config import Settings
from google_keyword_ai.storage.engine import open_database


def _set(cache: SqliteCache, key: str, payload: bytes = b"payload") -> None:
    cache.set(
        key,
        provider="autocomplete",
        endpoint="https://example.test",
        account_scope="",
        parser_version="1",
        payload=payload,
        ttl_seconds=60,
    )


def test_cache_key_depends_on_account_scope_and_parser_version() -> None:
    arguments = ("provider", "endpoint", {"q": "аренда"})
    base = build_cache_key(*arguments, account_scope="one", parser_version="1")

    assert base != build_cache_key(*arguments, account_scope="two", parser_version="1")
    assert base != build_cache_key(*arguments, account_scope="one", parser_version="2")


def test_cache_write_and_read(settings: Settings) -> None:
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)
        payload = "данные".encode()
        _set(cache, "key", payload)
        assert cache.get("key") == payload
    finally:
        engine.dispose()


def test_expired_entry_is_returned_as_miss_and_deleted(settings: Settings) -> None:
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)
        _set(cache, "expired")
        expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE cache_entries SET expires_at = ? WHERE key = ?",
                (expired_at, "expired"),
            )

        assert cache.get("expired") is None
        with engine.connect() as connection:
            count = connection.exec_driver_sql(
                "SELECT count(*) FROM cache_entries WHERE key = 'expired'"
            ).scalar_one()
        assert count == 0
    finally:
        engine.dispose()


def test_disabled_cache_does_not_read_or_write(data_dir: Path) -> None:
    enabled_settings = Settings(data_dir=data_dir)
    engine = open_database(enabled_settings)
    try:
        enabled = SqliteCache(engine, enabled_settings)
        _set(enabled, "existing")

        disabled = SqliteCache(engine, Settings(data_dir=data_dir, cache_enabled=False))
        assert disabled.get("existing") is None
        _set(disabled, "new")

        with engine.connect() as connection:
            keys = connection.exec_driver_sql("SELECT key FROM cache_entries").scalars().all()
        assert keys == ["existing"]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("not a timestamp", id="unparseable"),
        pytest.param("2026-01-01T00:00:00", id="parses_but_carries_no_zone"),
    ],
)
def test_an_expiry_that_cannot_be_judged_is_a_miss(settings: Settings, stored: str) -> None:
    """A damaged expiry has no knowable lifetime, so the entry is refetched.

    `fromisoformat` raises a bare `ValueError`, which is no `GkaiError`: one
    corrupt row would otherwise reach the caller as a crash instead of a miss,
    and every provider in the project reads its cache through here.

    Parsing is not the only hazard. Everything written here carries a zone,
    because `set` derives it from `datetime.now(UTC)` — but a value that parses
    and happens to be naive gets past `fromisoformat` and raises `TypeError` at
    the comparison instead, which a guard against `ValueError` alone does not
    catch. The crash came back through the other half of the same line.
    """
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)
        _set(cache, "damaged")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE cache_entries SET expires_at = ? WHERE key = ?", (stored, "damaged")
            )

        assert cache.get("damaged") is None

        with engine.connect() as connection:
            remaining = connection.exec_driver_sql(
                "SELECT count(*) FROM cache_entries WHERE key = ?", ("damaged",)
            ).scalar_one()
        assert remaining == 0, "an entry that can never be judged fresh should not be kept"
    finally:
        engine.dispose()


def test_purge_expired_removes_only_dead_entries(settings: Settings) -> None:
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)
        for key in ("expired", "permanent", "fresh"):
            _set(cache, key)
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE cache_entries SET expires_at = ? WHERE key = ?", (past, "expired")
            )
            connection.exec_driver_sql(
                "UPDATE cache_entries SET expires_at = NULL WHERE key = ?", ("permanent",)
            )
            connection.exec_driver_sql(
                "UPDATE cache_entries SET expires_at = ? WHERE key = ?", (future, "fresh")
            )

        assert cache.purge_expired() == 1
        with engine.connect() as connection:
            keys = set(connection.exec_driver_sql("SELECT key FROM cache_entries").scalars())
        assert keys == {"permanent", "fresh"}
    finally:
        engine.dispose()


def test_purge_expired_uses_batches_of_at_most_500(settings: Settings) -> None:
    engine = open_database(settings)
    try:
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        now = datetime.now(UTC).isoformat()
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO cache_entries (
                    key, provider, endpoint, account_scope, parser_version,
                    payload, created_at, expires_at
                ) VALUES (?, 'test', 'https://example.test', '', '1', ?, ?, ?)
                """,
                [(f"expired-{index}", b"x", now, past) for index in range(501)],
            )

        batch_sizes: list[int] = []

        def record_delete_batch(
            _connection: object,
            _cursor: object,
            statement: str,
            parameters: object,
            _context: object,
            executemany: bool,
        ) -> None:
            if statement.lstrip().startswith("DELETE FROM cache_entries"):
                if executemany:
                    assert isinstance(parameters, list)
                    batch_sizes.append(len(parameters))
                else:
                    batch_sizes.append(1)

        event.listen(engine, "before_cursor_execute", record_delete_batch)
        try:
            assert SqliteCache(engine, settings).purge_expired() == 501
        finally:
            event.remove(engine, "before_cursor_execute", record_delete_batch)

        assert batch_sizes == [500, 1]
    finally:
        engine.dispose()


@pytest.mark.parametrize("stored", ["not a timestamp", "2026-01-01T00:00:00"])
def test_purge_expired_agrees_with_get(settings: Settings, stored: str) -> None:
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)
        _set(cache, "damaged")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE cache_entries SET expires_at = ? WHERE key = ?", (stored, "damaged")
            )

        assert cache.purge_expired() == 1
        assert cache.get("damaged") is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("operation", ["purge", "get"])
def test_refreshed_entry_survives_expired_deletion(settings: Settings, operation: str) -> None:
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)
        _set(cache, "shared", b"stale")
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE cache_entries SET expires_at = ? WHERE key = ?", (past, "shared")
            )

        refreshed = False

        def refresh_before_delete(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            nonlocal refreshed
            if refreshed or not statement.lstrip().startswith("DELETE FROM cache_entries"):
                return
            refreshed = True
            _set(SqliteCache(engine, settings), "shared", b"fresh")

        event.listen(engine, "before_cursor_execute", refresh_before_delete)
        try:
            if operation == "purge":
                assert cache.purge_expired() == 0
            else:
                assert cache.get("shared") is None
        finally:
            event.remove(engine, "before_cursor_execute", refresh_before_delete)

        assert refreshed is True
        assert cache.get("shared") == b"fresh"
    finally:
        engine.dispose()


def test_sweep_runs_once_per_instance(settings: Settings) -> None:
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)
        _set(cache, "first")
        _set(cache, "expired")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE cache_entries SET expires_at = ? WHERE key = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "expired"),
            )

        _set(cache, "second")

        with engine.connect() as connection:
            keys = set(connection.exec_driver_sql("SELECT key FROM cache_entries").scalars())
        assert "expired" in keys
    finally:
        engine.dispose()


def test_sweep_can_be_disabled(data_dir: Path) -> None:
    settings = Settings(data_dir=data_dir, cache_sweep_enabled=False)
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)
        _set(cache, "expired")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE cache_entries SET expires_at = ? WHERE key = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "expired"),
            )

        _set(cache, "new")

        assert cache.counts().entries == 2
        assert cache.counts().expired_entries == 1
    finally:
        engine.dispose()


def test_maintenance_ignores_cache_enabled(data_dir: Path) -> None:
    enabled_settings = Settings(data_dir=data_dir)
    engine = open_database(enabled_settings)
    try:
        enabled = SqliteCache(engine, enabled_settings)
        _set(enabled, "expired")
        _set(enabled, "fresh", b"longer")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE cache_entries SET expires_at = ? WHERE key = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "expired"),
            )

        disabled = SqliteCache(engine, Settings(data_dir=data_dir, cache_enabled=False))
        assert disabled.counts().model_dump() == {
            "entries": 2,
            "expired_entries": 1,
            "payload_bytes": 13,
        }
        assert disabled.purge_expired() == 1
        assert disabled.purge_all() == 1
        assert disabled.counts().model_dump() == {
            "entries": 0,
            "expired_entries": 0,
            "payload_bytes": 0,
        }
    finally:
        engine.dispose()


def test_sweep_failure_does_not_break_set(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)
        attempts = 0

        def fail_sweep() -> int:
            nonlocal attempts
            attempts += 1
            raise SQLAlchemyError("sweep failed")

        monkeypatch.setattr(cache, "purge_expired", fail_sweep)

        _set(cache, "saved")
        _set(cache, "also-saved")

        with engine.connect() as connection:
            keys = set(connection.exec_driver_sql("SELECT key FROM cache_entries").scalars())
        assert attempts == 1
        assert keys == {"saved", "also-saved"}
    finally:
        engine.dispose()
