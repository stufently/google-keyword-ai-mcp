from datetime import UTC, datetime, timedelta
from pathlib import Path

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


def test_an_unreadable_expiry_is_a_miss_rather_than_a_crash(settings: Settings) -> None:
    """A damaged expiry has no knowable lifetime, so the entry is refetched.

    `fromisoformat` raises a bare `ValueError`, which is no `GkaiError`: one
    corrupt row would otherwise reach the caller as a crash instead of a miss,
    and every provider in the project reads its cache through here.
    """
    engine = open_database(settings)
    try:
        cache = SqliteCache(engine, settings)
        _set(cache, "damaged")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE cache_entries SET expires_at = ? WHERE key = ?",
                ("not a timestamp", "damaged"),
            )

        assert cache.get("damaged") is None

        with engine.connect() as connection:
            remaining = connection.exec_driver_sql(
                "SELECT count(*) FROM cache_entries WHERE key = ?", ("damaged",)
            ).scalar_one()
        assert remaining == 0, "an entry that can never be judged fresh should not be kept"
    finally:
        engine.dispose()
