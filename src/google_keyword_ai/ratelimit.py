import fcntl
import time
from pathlib import Path
from typing import TextIO

import anyio

from google_keyword_ai.errors import InvalidConfigurationError


class AsyncRateLimiter:
    def __init__(self, rate_per_second: float) -> None:
        if rate_per_second <= 0:
            raise InvalidConfigurationError("rate_per_second must be positive.")
        self._minimum_interval = 1.0 / rate_per_second
        self._last_issued_at: float | None = None
        self._lock = anyio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = anyio.current_time()
            if self._last_issued_at is not None:
                remaining = self._minimum_interval - (now - self._last_issued_at)
                if remaining > 0:
                    await anyio.sleep(remaining)
            self._last_issued_at = anyio.current_time()


def _parse_timestamp(stored: str) -> float | None:
    """Read the timestamp of the last issued request from the lock file.

    An empty file means no request has been issued yet. A damaged one means the
    opposite is unknown -- and `float` would raise a bare `ValueError` there,
    which is no `GkaiError` and would surface as a crash. Reading it as "one was
    just issued" costs at most a single interval of waiting, where reading it as
    "none yet" would let the call straight through and break the very limit the
    file exists to hold.
    """
    if not stored:
        return None
    try:
        return float(stored)
    except ValueError:
        return time.time()


class InterProcessRateLimiter:
    def __init__(self, rate_per_second: float, lock_path: Path) -> None:
        if rate_per_second <= 0:
            raise InvalidConfigurationError("rate_per_second must be positive.")
        self._minimum_interval = 1.0 / rate_per_second
        self._lock_path = lock_path

    def _open_and_lock(self) -> tuple[TextIO, float | None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self._lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            lock_file.seek(0)
            stored = lock_file.read().strip()
            return lock_file, _parse_timestamp(stored)
        except BaseException:
            lock_file.close()
            raise

    @staticmethod
    def _record(lock_file: TextIO, issued_at: float) -> None:
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"{issued_at:.9f}\n")
        lock_file.flush()

    @staticmethod
    def _unlock(lock_file: TextIO) -> None:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    async def acquire(self) -> None:
        lock_file, last_issued_at = await anyio.to_thread.run_sync(self._open_and_lock)
        try:
            now = time.time()
            earliest = now if last_issued_at is None else last_issued_at + self._minimum_interval
            if earliest > now:
                await anyio.sleep(earliest - now)
            issued_at = max(time.time(), earliest, last_issued_at or 0.0)
            await anyio.to_thread.run_sync(self._record, lock_file, issued_at)
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self._unlock, lock_file)
