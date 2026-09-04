from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from structlog.testing import capture_logs

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


def test_set_stamps_last_read_at_and_refreshes_both_timestamps(settings: Settings) -> None:
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)
        _set(cache, "stamped", b"first")
        with engine.connect() as connection:
            initial_created_at, initial_last_read_at = connection.exec_driver_sql(
                "SELECT created_at, last_read_at FROM cache_entries WHERE key = ?",
                ("stamped",),
            ).one()
        assert initial_created_at == initial_last_read_at

        old = "2020-01-01T00:00:00+00:00"
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE cache_entries SET created_at = ?, last_read_at = ? WHERE key = ?",
                (old, old, "stamped"),
            )

        _set(cache, "stamped", b"second")

        with engine.connect() as connection:
            created_at, last_read_at = connection.exec_driver_sql(
                "SELECT created_at, last_read_at FROM cache_entries WHERE key = ?",
                ("stamped",),
            ).one()
        assert created_at == last_read_at
        assert created_at != old
    finally:
        engine.dispose()


def test_get_touches_last_read_at(settings: Settings) -> None:
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)
        _set(cache, "touched")
        old = "2020-01-01T00:00:00+00:00"
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE cache_entries SET last_read_at = ? WHERE key = ?", (old, "touched")
            )

        assert cache.get("touched") == b"payload"

        with engine.connect() as connection:
            last_read_at = connection.exec_driver_sql(
                "SELECT last_read_at FROM cache_entries WHERE key = ?", ("touched",)
            ).scalar_one()
        assert datetime.fromisoformat(last_read_at) > datetime.fromisoformat(old)
    finally:
        engine.dispose()


def test_touch_failure_still_returns_payload(settings: Settings) -> None:
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)
        _set(cache, "still-a-hit")

        def fail_touch(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if statement.lstrip().startswith("UPDATE cache_entries SET last_read_at"):
                raise SQLAlchemyError("touch failed")

        event.listen(engine, "before_cursor_execute", fail_touch)
        try:
            with capture_logs() as logs:
                result = cache.get("still-a-hit")
        finally:
            event.remove(engine, "before_cursor_execute", fail_touch)

        assert result == b"payload"
        assert any(log.get("event") == "cache_touch_failed" for log in logs)
    finally:
        engine.dispose()


def test_evicts_least_recently_read_entries(settings: Settings) -> None:
    limited = settings.model_copy(update={"cache_max_bytes": 4, "cache_sweep_enabled": False})
    engine = open_database(limited)
    try:
        cache = SqliteCache(engine, limited)
        for index in range(5):
            _set(cache, f"key-{index}", b"xx")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE cache_entries SET last_read_at = ? WHERE key = ?",
                [
                    (
                        None if index == 0 else f"2026-09-04T00:00:0{index}+00:00",
                        f"key-{index}",
                    )
                    for index in range(5)
                ],
            )

        assert cache.evict_over_limit() == 3

        with engine.connect() as connection:
            remaining = set(connection.exec_driver_sql("SELECT key FROM cache_entries").scalars())
        assert remaining == {"key-3", "key-4"}
    finally:
        engine.dispose()


def test_touched_entry_survives_eviction(settings: Settings) -> None:
    limited = settings.model_copy(update={"cache_max_bytes": 4, "cache_sweep_enabled": False})
    engine = open_database(limited)
    try:
        cache = SqliteCache(engine, limited)
        for key in ("old", "recent", "newest"):
            _set(cache, key, b"xx")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE cache_entries SET last_read_at = ? WHERE key = ?",
                [
                    ("2026-09-04T00:00:01+00:00", "old"),
                    ("2026-09-04T00:00:02+00:00", "recent"),
                    ("2026-09-04T00:00:03+00:00", "newest"),
                ],
            )

        touched = False

        def touch_before_delete(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            nonlocal touched
            if touched or not statement.lstrip().startswith("DELETE FROM cache_entries"):
                return
            touched = True
            assert cache.get("old") == b"xx"

        event.listen(engine, "before_cursor_execute", touch_before_delete)
        try:
            assert cache.evict_over_limit() == 0
        finally:
            event.remove(engine, "before_cursor_execute", touch_before_delete)

        with engine.connect() as connection:
            remaining = set(connection.exec_driver_sql("SELECT key FROM cache_entries").scalars())
        assert touched is True
        assert "old" in remaining
    finally:
        engine.dispose()


def test_many_writes_stay_under_limit(settings: Settings) -> None:
    limited = settings.model_copy(update={"cache_max_bytes": 100})
    engine = open_database(limited)
    try:
        cache = SqliteCache(engine, limited)
        for index in range(101):
            _set(cache, f"key-{index}", b"x" * 10)

        slack = max(1, limited.cache_max_bytes // 100)
        assert cache.counts().payload_bytes <= limited.cache_max_bytes + slack
    finally:
        engine.dispose()


def test_eviction_disabled_by_zero(settings: Settings) -> None:
    unlimited = settings.model_copy(update={"cache_max_bytes": 0})
    engine = open_database(unlimited)
    try:
        cache = SqliteCache(engine, unlimited)
        for index in range(20):
            _set(cache, f"key-{index}", b"x" * 10)

        assert cache.evict_over_limit() == 0
        assert cache.counts().payload_bytes == 200
    finally:
        engine.dispose()


def test_sweep_switch_disables_both_maintenance_kinds(data_dir: Path) -> None:
    settings = Settings(data_dir=data_dir, cache_max_bytes=4, cache_sweep_enabled=False)
    engine = open_database(settings)
    try:
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        now = datetime.now(UTC).isoformat()
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO cache_entries (
                    key, provider, endpoint, account_scope, parser_version,
                    payload, created_at, expires_at, last_read_at
                ) VALUES (?, 'test', 'https://example.test', '', '1', ?, ?, ?, ?)
                """,
                ("expired", b"xx", now, past, now),
            )

        cache = SqliteCache(engine, settings)
        for index in range(5):
            _set(cache, f"new-{index}", b"xx")

        with engine.connect() as connection:
            keys = set(connection.exec_driver_sql("SELECT key FROM cache_entries").scalars())
        assert "expired" in keys
        assert cache.counts().payload_bytes > settings.cache_max_bytes
    finally:
        engine.dispose()


def test_cache_disabled_keeps_expired_entry_and_skips_new_write(data_dir: Path) -> None:
    enabled_settings = Settings(data_dir=data_dir)
    engine = open_database(enabled_settings)
    try:
        past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        now = datetime.now(UTC).isoformat()
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO cache_entries (
                    key, provider, endpoint, account_scope, parser_version,
                    payload, created_at, expires_at, last_read_at
                ) VALUES (?, 'test', 'https://example.test', '', '1', ?, ?, ?, ?)
                """,
                ("expired", b"old", now, past, now),
            )

        disabled = SqliteCache(
            engine, Settings(data_dir=data_dir, cache_enabled=False, cache_max_bytes=1)
        )
        _set(disabled, "new", b"new")

        with engine.connect() as connection:
            keys = set(connection.exec_driver_sql("SELECT key FROM cache_entries").scalars())
        assert keys == {"expired"}
    finally:
        engine.dispose()


def test_sweep_failure_is_logged(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)

        def fail_sweep() -> int:
            raise SQLAlchemyError("sweep failed")

        monkeypatch.setattr(cache, "purge_expired", fail_sweep)

        with capture_logs() as logs:
            _set(cache, "saved")

        assert any(
            log.get("event") == "cache_sweep_failed" and log.get("error") == "sweep failed"
            for log in logs
        )
    finally:
        engine.dispose()


def test_eviction_failure_is_best_effort(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    limited = settings.model_copy(update={"cache_max_bytes": 1})
    engine = open_database(limited)
    try:
        cache = SqliteCache(engine, limited)

        def fail_eviction() -> int:
            raise SQLAlchemyError("eviction failed")

        monkeypatch.setattr(cache, "evict_over_limit", fail_eviction)

        with capture_logs() as logs:
            _set(cache, "saved")

        assert cache.get("saved") == b"payload"
        assert any(
            log.get("event") == "cache_eviction_failed" and log.get("error") == "eviction failed"
            for log in logs
        )
    finally:
        engine.dispose()


def test_first_set_purges_expired_before_evicting_live_entries(data_dir: Path) -> None:
    settings = Settings(data_dir=data_dir, cache_max_bytes=14)
    engine = open_database(settings)
    try:
        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO cache_entries (
                    key, provider, endpoint, account_scope, parser_version,
                    payload, created_at, expires_at, last_read_at
                ) VALUES (?, 'test', 'https://example.test', '', '1', ?, ?, ?, ?)
                """,
                [
                    (
                        "live-oldest",
                        b"x" * 10,
                        now.isoformat(),
                        (now + timedelta(hours=1)).isoformat(),
                        (now - timedelta(hours=2)).isoformat(),
                    ),
                    (
                        "expired-newest",
                        b"x" * 10,
                        now.isoformat(),
                        (now - timedelta(seconds=1)).isoformat(),
                        (now - timedelta(hours=1)).isoformat(),
                    ),
                ],
            )

        cache = SqliteCache(engine, settings)
        _set(cache, "new", b"x")

        with engine.connect() as connection:
            keys = set(connection.exec_driver_sql("SELECT key FROM cache_entries").scalars())
        assert keys == {"live-oldest", "new"}
    finally:
        engine.dispose()


def test_eviction_uses_batches_of_at_most_500(settings: Settings) -> None:
    limited = settings.model_copy(update={"cache_max_bytes": 1, "cache_sweep_enabled": False})
    engine = open_database(limited)
    try:
        cache = SqliteCache(engine, limited)
        for index in range(502):
            _set(cache, f"key-{index}", b"x")

        batch_sizes: list[int] = []

        def record_delete_batch(
            _connection: object,
            _cursor: object,
            statement: str,
            parameters: object,
            _context: object,
            executemany: bool,
        ) -> None:
            if not statement.lstrip().startswith("DELETE FROM cache_entries"):
                return
            if executemany:
                assert isinstance(parameters, list)
                batch_sizes.append(len(parameters))
            else:
                batch_sizes.append(1)

        event.listen(engine, "before_cursor_execute", record_delete_batch)
        try:
            assert cache.evict_over_limit() == 501
        finally:
            event.remove(engine, "before_cursor_execute", record_delete_batch)

        assert batch_sizes == [500, 1]
    finally:
        engine.dispose()
